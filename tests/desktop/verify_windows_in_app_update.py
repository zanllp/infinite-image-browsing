"""Run only on a disposable Windows runner, after the target NSIS build was verified."""
import functools
import hashlib
import http.server
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import ssl
import subprocess
import tempfile
import threading
import time
from urllib.request import urlopen

import psutil
from playwright.sync_api import sync_playwright
import trustme


def wait_for(check, description, timeout=150):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result := check():
            return result
        time.sleep(.5)
    raise RuntimeError(f"Timed out: {description}")


def app_process(exe):
    return next((p for p in psutil.process_iter(["exe"]) if p.info["exe"]
                 and Path(p.info["exe"]) == exe), None)


def verify(root, feed_dir):
    install = root / "installed"
    exe, = [p for p in install.glob("*.exe") if not re.match("iib_api_server|uninstall", p.name, re.I)]
    # verify_draft_asset.py installed the exact signed archive's target beforehand.
    target_hash = hashlib.sha256(exe.read_bytes()).digest()
    manifest_path, = feed_dir.glob("latest-windows-*.json")
    manifest = json.loads(manifest_path.read_text())
    entry = manifest["platforms"]["windows-x86_64"]
    archive = feed_dir / entry["url"].rsplit("/", 1)[1]
    assert archive.is_file() and entry["signature"]
    with tempfile.TemporaryDirectory() as work:
        work = Path(work)
        ca = trustme.CA()
        pem = ca.cert_pem.bytes().decode()
        thumbprint = hashlib.sha1(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()
        cert = work / "root.cer"
        cert.write_text(pem, encoding="ascii")
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ca.issue_cert("127.0.0.1").configure_cert(tls)
        public = work / "www"
        public.mkdir()
        shutil.copyfile(archive, public / archive.name)
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=public)
        with http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
            server.socket = tls.wrap_socket(server.socket, server_side=True)
            origin = f"https://127.0.0.1:{server.server_port}"
            entry["url"] = origin + "/" + archive.name
            (public / "feed.json").write_text(json.dumps(manifest))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            subprocess.run(["certutil", "-user", "-addstore", "Root", str(cert)], check=True)
            try:
                run_upgrade(exe, root, feed_dir, origin, manifest["version"], target_hash)
            finally:
                server.shutdown()
                proc = app_process(exe)
                if proc:
                    proc.kill()
                    proc.wait(15)
                subprocess.run(["certutil", "-user", "-delstore", "Root", thumbprint], check=True)


def run_upgrade(exe, root, feed_dir, origin, version, target_hash):
    vue = Path("vue").resolve()
    old = "0.0.1"
    config = json.loads((vue / "updater.json").read_text())
    config.update(package={"version": old}, build={"beforeBuildCommand": ""})
    config["tauri"]["updater"]["endpoints"] = [origin + "/feed.json"]
    (vue / "baseline.json").write_text(json.dumps(config))
    subprocess.run(["node", "node_modules/@tauri-apps/cli/tauri.js", "build", "--features",
                    "desktop-updater", "--bundles", "nsis", "--config", "baseline.json"],
                   cwd=vue, check=True)
    baseline = feed_dir / f"Infinite Image Browsing_{old}_x64-setup.exe"
    subprocess.run(f'"{baseline}" /S /D={exe.parent}', check=True, timeout=180)
    assert hashlib.sha256(exe.read_bytes()).digest() != target_hash, "Baseline did not install"
    assert app_process(exe) is None
    (exe.parent / "app.conf.json").write_text('{"sdwebui_dir":""}')
    with sqlite3.connect(exe.parent / "iib.db") as db:
        db.execute("CREATE TABLE updater_test (marker TEXT)")
        db.execute("INSERT INTO updater_test VALUES ('keep my library')")
    startup_log = exe.parent / "iib_api_server.log"
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {**os.environ, "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS": f"--remote-debugging-port={port}"}
    first = subprocess.Popen([str(exe)], cwd=exe.parent, env=env)
    def debugger_ready():
        if first.poll() is not None:
            raise RuntimeError(f"Baseline exited before update: {first.returncode}")
        try:
            with urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2) as r:
                return any(p.get("type") == "page" for p in json.load(r))
        except OSError:
            return False
    wait_for(debugger_ready, "baseline WebView2")
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        page = browser.contexts[0].pages[0]
        button = page.get_by_role("button", name="Update now", exact=True)
        button.wait_for(timeout=150_000)
        log_offset = startup_log.stat().st_size
        button.click(timeout=30_000)
        print("Clicked Update now in the baseline app", flush=True)
    first.wait(timeout=180)
    second = wait_for(lambda: app_process(exe), "automatic relaunch")
    assert second.pid != first.pid and hashlib.sha256(exe.read_bytes()).digest() == target_hash
    def restarted():
        with startup_log.open("rb") as log_file:
            log_file.seek(log_offset)
            log = log_file.read().decode("utf-8", errors="replace")
        launches = log.split("event=app_start")
        if len(launches) >= 2 and "name=fe_loaded" in launches[-1]:
            return re.search(r"event=launch_app port=(\d+)", launches[-1])[1]
    port = wait_for(restarted, "updated WebView and sidecar")
    with urlopen(f"http://127.0.0.1:{port}/infinite_image_browsing/global_setting", timeout=10) as r:
        assert Path(json.load(r)["cwd"]).resolve() == exe.parent
    with sqlite3.connect(exe.parent / "iib.db") as db:
        assert db.execute("SELECT marker FROM updater_test").fetchone() == ("keep my library",)
    result = dict(baseline=old, target=version, old_pid=first.pid, relaunched_pid=second.pid,
                  target_binary_verified=True, webview_ready=True, database_preserved=True)
    (root / "in-app-upgrade.json").write_text(json.dumps(result, indent=2))
    print("PASS:", result, flush=True)


if __name__ == "__main__":
    verify(Path("verification").resolve(), Path("vue/src-tauri/target/release/bundle/nsis").resolve())
