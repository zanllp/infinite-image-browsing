"""Install and exercise an asset downloaded from a draft release."""

import ctypes
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import time
from urllib.request import urlopen
import uuid

from smoke_test_desktop import smoke_test, smoke_test_app


def run(args, **kwargs):
    result = subprocess.run(args, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, **kwargs)
    print(result.stdout, flush=True)
    return result


def windows_process_running(pid):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x00100000, False, pid)
    if not handle:
        return False
    try:
        return kernel.WaitForSingleObject(handle, 0) == 0x102
    finally:
        kernel.CloseHandle(handle)


def verify_windows(asset, root, report):
    if asset.name.startswith("iib_app_cli-"):
        smoke_test(asset)
        report["standalone_cli"] = "passed: resources, database, media operations, shutdown"
        return
    install = root / "installed"
    # NSIS /D consumes the remainder of the command line and must come last.
    result = run(f'"{asset}" /S /D={install}', timeout=180)
    assert result.returncode == 0, f"Installer failed: {result.returncode}"
    executables = [p for p in install.rglob("*.exe")
                   if p.name.lower() != "iib_api_server.exe" and "uninstall" not in p.name.lower()]
    assert len(executables) == 1, f"Unexpected installed executables: {executables}"
    backend = install / "iib_api_server.exe"
    assert backend.is_file(), "Installer did not contain the sidecar"
    data = root / "app-data"
    data.mkdir()
    startup_log = data / "iib_api_server.log"
    child_pid = None
    with (root / "app.log").open("w+") as log:
        process = subprocess.Popen([str(executables[0])], cwd=data, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 120
            while True:
                if process.poll() is not None:
                    raise RuntimeError(f"Installed app exited: {process.returncode}")
                text = startup_log.read_text() if startup_log.exists() else ""
                port = re.search(r"event=launch_app port=(\d+)", text)
                sidecar = re.search(r"event=sidecar_spawned pid=(\d+)", text)
                if sidecar:
                    child_pid = int(sidecar[1])
                if port and "name=fe_loaded" in text:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("Installed Windows app did not become ready")
                time.sleep(.5)
            with urlopen(f"http://127.0.0.1:{port[1]}/infinite_image_browsing/global_setting", timeout=10) as response:
                settings = json.load(response)
            assert Path(settings["cwd"]).resolve() == data.resolve()
            assert (data / "iib.db").is_file()
            assert child_pid is not None
            process.terminate()
            process.wait(timeout=10)
            deadline = time.monotonic() + 10
            while windows_process_running(child_pid):
                if time.monotonic() >= deadline:
                    raise RuntimeError("Installed Windows app left its sidecar running")
                time.sleep(.2)
            report["installed_application"] = "passed: WebView ready, database, sidecar cleanup"
        finally:
            log.flush()
            log.seek(0)
            print(log.read())
            if startup_log.exists():
                shutil.copy2(startup_log, root / "startup.log")
            for pid in [process.pid, child_pid]:
                if pid and windows_process_running(pid):
                    run(["taskkill", "/F", "/T", "/PID", str(pid)])
            process.wait()
    smoke_test(backend)
    report["installed_sidecar"] = "passed: HTML/JS, database, thumbnails, video, shutdown"
    report["windows_security_scope"] = "Silent installer/native process test; SmartScreen UI not tested"


def verify_macos(asset, root, report):
    run(["hdiutil", "verify", str(asset)], check=True)
    mount = root / "mount"
    mount.mkdir()
    run(["hdiutil", "attach", str(asset), "-readonly", "-nobrowse", "-mountpoint", str(mount)], check=True)
    try:
        apps = list(mount.glob("*.app"))
        assert len(apps) == 1, f"Unexpected applications in DMG: {apps}"
        app = root / apps[0].name
        run(["ditto", str(apps[0]), str(app)], check=True)
    finally:
        run(["hdiutil", "detach", str(mount)], check=True)
    run(["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app)], check=True)
    report["bundle_integrity"] = "passed"
    report["gatekeeper_status"] = run(["spctl", "--status"]).stdout.strip()
    quarantine = f"0081;{int(time.time()):x};IIBDraftVerification;{uuid.uuid4()}"
    run(["xattr", "-r", "-w", "com.apple.quarantine", quarantine, str(app)], check=True)
    assessment = run(["spctl", "--assess", "--type", "execute", "--verbose=4", str(app)])
    report["quarantined_assessment"] = {"exit_code": assessment.returncode, "output": assessment.stdout}
    home = root / "quarantined-home"
    home.mkdir()
    stdout, stderr = root / "quarantine.stdout", root / "quarantine.stderr"
    launcher = subprocess.Popen(["open", "-n", "-W", "-g", str(app),
                                 "--env", f"HOME={home}", "--stdout", str(stdout), "--stderr", str(stderr)],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    ready = False
    log_path = home / "Library/Application Support/com.zanllp.iib/iib_api_server.log"
    try:
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if log_path.exists() and "name=fe_loaded" in log_path.read_text():
                ready = True
                break
            if launcher.poll() is not None:
                break
            time.sleep(.5)
        report["quarantined_launch"] = "WebView ready" if ready else "not ready: blocked/prompted or launch error"
        run(["screencapture", "-x", str(root / "quarantined-launch.png")])
    finally:
        config = plistlib.loads((app / "Contents/Info.plist").read_bytes())
        executable = str(app / "Contents/MacOS" / config["CFBundleExecutable"])
        processes = subprocess.check_output(["ps", "-axo", "pid=,command="], text=True)
        for line in processes.splitlines():
            parts = line.strip().split(maxsplit=1)
            if len(parts) == 2 and parts[1] == executable:
                run(["kill", "-TERM", parts[0]])
        if launcher.poll() is None:
            launcher.terminate()
        output = launcher.communicate(timeout=10)[0]
        report["launch_services"] = {"exit_code": launcher.returncode, "output": output}
        if log_path.exists():
            shutil.copy2(log_path, root / "quarantined-startup.log")
    if assessment.returncode or not ready:
        print("::warning::Downloaded unsigned macOS app is not a frictionless first install; see quarantine report.")
    # Explicitly test a per-app exception only, without changing system policy.
    run(["xattr", "-dr", "com.apple.quarantine", str(app)], check=True)
    smoke_test_app(app)
    smoke_test(app / "Contents/MacOS/iib_api_server")
    report["after_per_app_exception"] = "passed: full application and packaged sidecar"


if __name__ == "__main__":
    asset = Path(sys.argv[1]).resolve()
    root = Path(sys.argv[2]).resolve()
    root.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(asset.read_bytes()).hexdigest()
    report = {"asset": asset.name, "sha256": digest, "platform": sys.platform}
    try:
        if sys.platform == "win32":
            verify_windows(asset, root, report)
        else:
            verify_macos(asset, root, report)
        report["functional_result"] = "passed"
    except BaseException as error:
        report["functional_result"] = "failed"
        report["error"] = repr(error)
        raise
    finally:
        (root / "report.json").write_text(json.dumps(report, indent=2))
        print(json.dumps(report, indent=2))
