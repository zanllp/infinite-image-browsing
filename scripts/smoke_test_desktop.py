"""Exercise the packaged sidecar, not the source checkout or an installed Python."""

import json
from http.client import RemoteDisconnected
import os
from pathlib import Path
import plistlib
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import av
from PIL import Image


def smoke_test_app(app):
    with tempfile.TemporaryDirectory(prefix="iib app smoke ") as directory:
        home = Path(directory)
        data = home / "Library/Application Support/com.zanllp.iib"
        startup_log = data / "iib_api_server.log"
        config = plistlib.loads((app / "Contents/Info.plist").read_bytes())
        with (home / "app.log").open("w+") as log:
            process = subprocess.Popen(
                [str(app / "Contents/MacOS" / config["CFBundleExecutable"])],
                cwd="/", env={**os.environ, "HOME": str(home)},
                stdout=log, stderr=log, start_new_session=True,
            )
            try:
                deadline = time.monotonic() + 90
                while True:
                    if process.poll() is not None:
                        raise RuntimeError(f"App exited before ready: {process.returncode}")
                    text = startup_log.read_text() if startup_log.exists() else ""
                    port = re.search(r"event=launch_app port=(\d+)", text)
                    if port and "name=fe_loaded" in text:
                        break
                    if time.monotonic() >= deadline:
                        raise RuntimeError("The app's WebView did not finish loading")
                    time.sleep(0.5)
                with urlopen(f"http://127.0.0.1:{port[1]}/infinite_image_browsing/global_setting", timeout=10) as response:
                    settings = json.load(response)
                assert Path(settings["cwd"]).resolve() == data.resolve()
                assert (data / "iib.db").is_file()
                sidecar = re.search(r"event=sidecar_spawned pid=(\d+)", text)
                assert sidecar, "No bundled sidecar process was recorded"
                process.terminate()
                process.wait(timeout=10)
                deadline = time.monotonic() + 10
                while True:
                    try:
                        os.kill(int(sidecar[1]), 0)
                    except ProcessLookupError:
                        break
                    if time.monotonic() >= deadline:
                        raise RuntimeError("The sidecar survived the desktop app exiting")
                    time.sleep(0.2)
                print("PASS: full app launched from /, WebView ready, user data directory, no orphaned sidecar")
            except BaseException:
                log.flush()
                log.seek(0)
                print(log.read(), file=sys.stderr)
                if startup_log.exists():
                    print(startup_log.read_text(), file=sys.stderr)
                raise
            finally:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()


def smoke_test(executable):
    with tempfile.TemporaryDirectory(prefix="iib desktop smoke ") as directory:
        root = Path(directory)
        image_path = root / "sample.png"
        Image.new("RGB", (64, 64), "red").save(image_path)
        video_path = root / "sample.mp4"
        with av.open(str(video_path), "w") as container:
            stream = container.add_stream("mpeg4", rate=24)
            stream.width = stream.height = 64
            stream.pix_fmt = "yuv420p"
            for _ in range(24):
                frame = av.VideoFrame.from_image(Image.open(image_path))
                for packet in stream.encode(frame):
                    container.mux(packet)
            for packet in stream.encode():
                container.mux(packet)

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        base = f"http://127.0.0.1:{port}"

        def get(path):
            with urlopen(base + path, timeout=60) as response:
                return response.read()

        with (root / "sidecar.log").open("w+") as log:
            process = subprocess.Popen(
                [str(executable), "--port", str(port), "--enable_shutdown",
                 "--parent_pid", str(os.getpid())],
                cwd=root, stdout=log, stderr=log, start_new_session=True,
            )
            try:
                deadline = time.monotonic() + 60
                while True:
                    if process.poll() is not None:
                        raise RuntimeError(f"Sidecar exited before ready: {process.returncode}")
                    try:
                        assert json.loads(get("/infinite_image_browsing/hello")) == "hello"
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise RuntimeError("Sidecar did not become ready within 60 seconds")
                        time.sleep(0.5)

                html = get("/").decode()
                asset = re.search(r'src="([^"]+\.js)"', html)
                assert asset, "The packaged homepage has no JavaScript entry"
                assert get(asset.group(1)), "The packaged JavaScript entry is empty"
                settings = json.loads(get("/infinite_image_browsing/global_setting"))
                assert Path(settings["cwd"]).resolve() == root.resolve()
                assert (root / "iib.db").is_file(), "Database was not created in the writable directory"
                for endpoint, query in (
                    ("image-thumbnail", {"path": str(image_path), "t": "smoke"}),
                    ("video_cover", {"path": str(video_path), "mt": "smoke"}),
                ):
                    assert get(f"/infinite_image_browsing/{endpoint}?{urlencode(query)}")
                try:
                    with urlopen(Request(base + "/infinite_image_browsing/shutdown", method="POST"), timeout=5):
                        pass
                except (RemoteDisconnected, ConnectionResetError):
                    # The existing shutdown endpoint sends SIGKILL before responding.
                    pass
                code = process.wait(timeout=15)
                expected = 9 if sys.platform == "win32" else -signal.SIGKILL
                assert code == expected, f"Unexpected shutdown exit code: {code}"
                print("PASS: packaged sidecar, HTML/JS, database, image thumbnail, video cover, shutdown")
            except BaseException:
                log.flush()
                log.seek(0)
                print(log.read(), file=sys.stderr)
                backend_log = root / "log.log"
                if backend_log.exists():
                    print(backend_log.read_text(), file=sys.stderr)
                raise
            finally:
                if process.poll() is None:
                    if sys.platform == "win32":
                        subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)], check=False)
                    else:
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait()


if __name__ == "__main__":
    if sys.argv[1] == "--app":
        smoke_test_app(Path(sys.argv[2]).resolve())
    else:
        smoke_test(Path(sys.argv[1]).resolve())
