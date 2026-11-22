"""Build Tauri v1 update feeds from the final, smoke-tested release assets."""
import argparse
import base64
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[2]
# The alternative Windows installer deliberately has a separate feed.
TARGETS = {
    "windows-x86_64": "windows-x86_64",
    "windows-x86_64-pyinstaller": "windows-x86_64",
    "darwin-aarch64": "darwin-aarch64",
    "darwin-x64": "darwin-x86_64",
}


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def key_id(armored):
    lines = base64.b64decode(armored, validate=True).decode().splitlines()
    if len(lines) < 2:
        raise ValueError("Invalid Tauri signing key or signature")
    packet = base64.b64decode(lines[1], validate=True)
    if len(packet) < 10:
        raise ValueError("Missing minisign key ID")
    return packet[2:10]


def configure(channel, output):
    pubkey = os.environ["IIB_UPDATER_PUBLIC_KEY"].strip()
    key_id(pubkey)  # Reject a missing or malformed release variable before compiling.
    repo = os.environ["GITHUB_REPOSITORY"]
    write_json(output, {"tauri": {"updater": {
        "active": True, "dialog": False, "pubkey": pubkey,
        "endpoints": [f"https://github.com/{repo}/releases/latest/download/latest-{channel}.json"],
        "windows": {"installMode": "passive"}
    }}})


def archive(source, dest):
    if source.suffix == ".exe":
        # Tauri 1.4's ZIP reader supports stored NSIS installers.
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_STORED) as bundle:
            bundle.write(source, source.name)
    elif source.suffix == ".app" and (source / "Contents/Resources/iib-backend/iib_api_server").is_file():
        with tarfile.open(dest, "w:gz", format=tarfile.GNU_FORMAT) as bundle:
            bundle.add(source / "Contents", arcname=f"{source.name}/Contents")
    else:
        raise ValueError(f"Not a desktop release asset: {source}")


def package(channel, source, output):
    version = json.loads((ROOT / "vue/src-tauri/tauri.conf.json").read_text())["package"]["version"]
    ext = "nsis.zip" if channel.startswith("windows-") else "app.tar.gz"
    name = f"IIB-{version}-{channel}.{ext}"
    output.mkdir(parents=True, exist_ok=True)
    path = output / name
    archive(source, path)
    # Only this post-build step receives the private key, never Vite/Tauri build.
    with tempfile.TemporaryDirectory() as tmp:
        key = Path(tmp) / "signer.key"
        key.write_text(os.environ["IIB_UPDATER_PRIVATE_KEY"].strip(), encoding="ascii")
        signer = ROOT / "vue/node_modules/@tauri-apps/cli/tauri.js"
        result = subprocess.run(
            ["node", str(signer), "signer", "sign", "-f", str(key),
             "-p", os.environ.get("IIB_UPDATER_KEY_PASSWORD", ""), str(path)],
            stdout=subprocess.DEVNULL,
        )
    if result.returncode:
        # CalledProcessError would echo the password from argv.
        raise RuntimeError("Could not sign the desktop update")
    signature = Path(f"{path}.sig").read_text().strip()
    if key_id(signature) != key_id(os.environ["IIB_UPDATER_PUBLIC_KEY"].strip()):
        raise ValueError("Updater signing key does not match the embedded public key")
    repo = os.environ["GITHUB_REPOSITORY"]
    write_json(output / f"latest-{channel}.json", {
        "version": version,
        "platforms": {TARGETS[channel]: {
            "url": f"https://github.com/{repo}/releases/download/v{version}/{name}",
            "signature": signature,
        }}
    })


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    config = commands.add_parser("config")
    config.add_argument("channel", choices=TARGETS)
    config.add_argument("output", type=Path)
    pack = commands.add_parser("pack")
    pack.add_argument("channel", choices=TARGETS)
    pack.add_argument("source", type=Path)
    pack.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.command == "config":
        configure(args.channel, args.output)
    else:
        package(args.channel, args.source, args.output)
