import base64
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import zipfile

from tools.release import desktop_update as release


def armored(packet):
    return base64.b64encode(b"comment\n" + base64.b64encode(packet) + b"\n").decode()


class DesktopUpdateTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.key = b"12345678"
        env = patch.dict("os.environ", {
            "IIB_UPDATER_PUBLIC_KEY": armored(b"Ed" + self.key + bytes(32)),
            "IIB_UPDATER_PRIVATE_KEY": "test",
            "GITHUB_REPOSITORY": "example/iib",
        })
        env.start()
        self.addCleanup(env.stop)

    def test_channel_feed_matches_the_signed_archive(self):
        for channel, platform in release.TARGETS.items():
            with self.subTest(channel=channel):
                win = channel.startswith("windows-")
                source = self.root / ("IIB setup.exe" if win else "IIB.app")
                binary = source if win else source / "Contents/Resources/iib-backend/iib_api_server"
                binary.parent.mkdir(exist_ok=True, parents=True)
                binary.write_bytes(b"release asset")
                binary.chmod(0o755)
                if not win:
                    resource = binary.parent / "_internal/vue/dist/index.html"
                    resource.parent.mkdir(parents=True, exist_ok=True)
                    resource.write_bytes(b"packaged frontend")
                out = self.root / channel
                config = self.root / "config.json"
                release.configure(channel, config)

                def sign(argv, **_):
                    Path(argv[-1] + ".sig").write_text(armored(b"ED" + self.key + bytes(64)))
                    return SimpleNamespace(returncode=0)

                with patch.object(release.subprocess, "run", side_effect=sign):
                    release.package(channel, source, out)
                endpoint, = json.loads(config.read_text())["tauri"]["updater"]["endpoints"]
                feed = json.loads((out / endpoint.split("/")[-1]).read_text())
                entry = feed["platforms"][platform]
                path = out / entry["url"].split("/")[-1]
                self.assertIn("/releases/download/v" + feed["version"] + "/", entry["url"])
                self.assertEqual(entry["signature"], Path(str(path) + ".sig").read_text())
                if win:
                    with zipfile.ZipFile(path) as archive:
                        self.assertEqual(archive.namelist(), [source.name])
                        self.assertEqual(archive.getinfo(source.name).compress_type, zipfile.ZIP_STORED)
                        self.assertEqual(archive.read(source.name), binary.read_bytes())
                else:
                    with tarfile.open(path) as archive:
                        sidecar = archive.getmember("IIB.app/Contents/Resources/iib-backend/iib_api_server")
                        self.assertEqual(archive.extractfile(
                            f"{source.name}/{resource.relative_to(source).as_posix()}"
                        ).read(), b"packaged frontend")
                        self.assertTrue(sidecar.mode & 0o111)
                        self.assertEqual(archive.extractfile(sidecar).read(), binary.read_bytes())

    def test_signer_mismatch_does_not_publish_feed(self):
        source = self.root / "IIB.exe"
        source.write_bytes(b"NSIS")

        def wrong_signer(argv, **_):
            Path(argv[-1] + ".sig").write_text(armored(b"ED87654321" + bytes(64)))
            return SimpleNamespace(returncode=0)

        with patch.object(release.subprocess, "run", side_effect=wrong_signer):
            with self.assertRaisesRegex(ValueError, "does not match"):
                release.package("windows-x86_64", source, self.root)
        self.assertFalse((self.root / "latest-windows-x86_64.json").exists())

    def test_missing_public_key_fails_configuration(self):
        with patch.dict("os.environ", {"IIB_UPDATER_PUBLIC_KEY": ""}):
            with self.assertRaises(ValueError):
                release.configure("windows-x86_64", self.root / "config.json")
