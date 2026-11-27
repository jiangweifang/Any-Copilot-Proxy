from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path
import tarfile
import tempfile
import unittest

from aiohttp import web

from copilot_proxy.antigravity import AntigravityInstaller, default_binary_path, detect_platform
from copilot_proxy.config import ProxySettings, UserStore
from copilot_proxy.network import Network, UpstreamError
from copilot_proxy.runtime import Runtime

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from copilot_proxy.desktop import AntigravityDialog, MainWindow
    HAS_QT = True
except ImportError:
    HAS_QT = False


class AntigravityInstallerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "project"
        self.data = Path(self.temp.name) / "data"
        self.install_dir = Path(self.temp.name) / "bin"
        self.root.mkdir()
        self.store = UserStore(self.root, self.data)
        self.store.ensure()
        self.network = Network(ProxySettings())

        self.payload_bytes = b"MZ-FAKE-AGY-BINARY-" * 4096
        self.payload_sha512 = hashlib.sha512(self.payload_bytes).hexdigest()

        tar_buf = io.BytesIO()
        with tarfile.open(fileobj=tar_buf, mode="w:gz") as tar:
            info = tarfile.TarInfo("antigravity")
            info.size = len(self.payload_bytes)
            tar.addfile(info, io.BytesIO(self.payload_bytes))
        self.tar_bytes = tar_buf.getvalue()
        self.tar_sha512 = hashlib.sha512(self.tar_bytes).hexdigest()

        self.serve_mode = "binary"
        app = web.Application()
        app.router.add_get("/manifests/{platform}.json", self._handle_manifest)
        app.router.add_get("/redirect-binary", self._handle_redirect)
        app.router.add_get("/files/agy.bin", self._handle_binary)
        app.router.add_get("/files/agy.tar.gz", self._handle_tar)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        self.base_url = f"http://127.0.0.1:{self.port}"

    async def asyncTearDown(self):
        await self.runner.cleanup()
        await self.network.close()
        self.temp.cleanup()

    async def _handle_manifest(self, request: web.Request) -> web.Response:
        if self.serve_mode == "bad_sha":
            return web.json_response({
                "version": "1.9.0",
                "url": f"{self.base_url}/files/agy.bin",
                "sha512": "0" * 128,
            })
        if self.serve_mode == "tar":
            return web.json_response({
                "version": "1.9.1",
                "url": f"{self.base_url}/files/agy.tar.gz",
                "sha512": self.tar_sha512,
            })
        return web.json_response({
            "version": "1.9.0",
            "url": f"{self.base_url}/redirect-binary",
            "sha512": self.payload_sha512,
        })

    async def _handle_redirect(self, request: web.Request) -> web.Response:
        raise web.HTTPFound(location=f"{self.base_url}/files/agy.bin")

    async def _handle_binary(self, request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(headers={"Content-Length": str(len(self.payload_bytes))})
        await resp.prepare(request)
        mid = len(self.payload_bytes) // 2
        await resp.write(self.payload_bytes[:mid])
        await resp.write(self.payload_bytes[mid:])
        await resp.write_eof()
        return resp

    async def _handle_tar(self, request: web.Request) -> web.Response:
        return web.Response(body=self.tar_bytes, headers={"Content-Length": str(len(self.tar_bytes))})

    async def test_download_with_redirect_sha512_and_progress(self):
        installer = AntigravityInstaller(
            manifest_base_url=self.base_url,
            install_dir=self.install_dir,
            run_post_install=False,
        )
        result = await installer.install(self.network)
        self.assertEqual(result["state"], "complete")
        self.assertEqual(result["stage"], "complete")
        self.assertEqual(result["percent"], 100)
        self.assertEqual(result["version"], "1.9.0")
        self.assertEqual(result["downloadedBytes"], len(self.payload_bytes))
        self.assertEqual(result["totalBytes"], len(self.payload_bytes))
        expected_binary = default_binary_path(self.install_dir)
        self.assertTrue(expected_binary.is_file())
        self.assertEqual(expected_binary.read_bytes(), self.payload_bytes)

    async def test_tar_gz_extraction_install(self):
        self.serve_mode = "tar"
        installer = AntigravityInstaller(
            manifest_base_url=self.base_url,
            install_dir=self.install_dir,
            run_post_install=False,
        )
        result = await installer.install(self.network)
        self.assertEqual(result["state"], "complete")
        self.assertEqual(default_binary_path(self.install_dir).read_bytes(), self.payload_bytes)

    async def test_checksum_mismatch_aborts_and_does_not_write_binary(self):
        self.serve_mode = "bad_sha"
        installer = AntigravityInstaller(
            manifest_base_url=self.base_url,
            install_dir=self.install_dir,
            run_post_install=False,
        )
        with self.assertRaises(UpstreamError):
            await installer.install(self.network)
        self.assertEqual(installer.status()["state"], "failed")
        self.assertIn("SHA-512", installer.status()["error"])
        self.assertFalse(default_binary_path(self.install_dir).exists())

    async def test_runtime_agy_install_command(self):
        installer = AntigravityInstaller(
            manifest_base_url=self.base_url,
            install_dir=self.install_dir,
            run_post_install=False,
        )
        runtime = Runtime(self.store, agy_installer=installer, request_interval=0)
        try:
            await runtime.execute({"command": "agy-install"})
            assert runtime.agy_install_task is not None
            await runtime.agy_install_task
            status = runtime.status()
            self.assertEqual(status["antigravity"]["install"]["state"], "complete")
            self.assertEqual(status["antigravity"]["install"]["percent"], 100)
        finally:
            await runtime.close()

    def test_detect_platform_returns_valid_target(self):
        plat = detect_platform()
        self.assertIn(plat, ("windows_amd64", "windows_arm64", "darwin_amd64", "darwin_arm64", "linux_amd64", "linux_arm64"))


@unittest.skipUnless(HAS_QT, "Desktop extra is not installed")
class AntigravityDesktopInstallTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "project"
        self.data = Path(self.temp.name) / "data"
        self.root.mkdir()
        self.store = UserStore(self.root, self.data)
        self.store.ensure()
        self.window = MainWindow(self.store, testing=True)

    def tearDown(self):
        self.window.close()
        self.temp.cleanup()

    def _status(self, *, binary_found: bool, install: dict, authenticated: bool = False):
        return {
            "state": "signed-out",
            "message": "",
            "baseUrl": "http://127.0.0.1:4140/v1",
            "authenticated": authenticated,
            "githubAuthenticated": False,
            "loginState": "idle",
            "logging": {"enabled": True, "active": True},
            "m365": {"syncEnabled": True, "username": None},
            "antigravity": {
                "enabled": False,
                "binaryFound": binary_found,
                "authenticated": authenticated,
                "loginState": "idle",
                "account": None,
                "install": install,
            },
        }

    def test_install_button_progress_bar_and_completion_transition(self):
        # 1. Not installed -> shows Install agy CLI button, hides login button and progress bar
        self.window.completed("status", self._status(binary_found=False, install={"state": "idle", "stage": "idle", "percent": 0}))
        self.assertFalse(self.window.agy_install_button.isHidden())
        self.assertTrue(self.window.agy_login_button.isHidden())
        self.assertTrue(self.window.agy_progress_bar.isHidden())

        # 2. Downloading -> shows Cancel install button and progress bar with byte progress
        self.window.completed("status", self._status(binary_found=False, install={
            "state": "running",
            "stage": "downloading",
            "version": "1.9.0",
            "downloadedBytes": 10 * 1048576,
            "totalBytes": 40 * 1048576,
            "percent": 22,
        }))
        self.assertTrue(self.window.agy_install_button.isHidden())
        self.assertFalse(self.window.agy_cancel_install_button.isHidden())
        self.assertFalse(self.window.agy_progress_bar.isHidden())
        self.assertEqual(self.window.agy_progress_bar.value(), 22)
        self.assertIn("10.0 / 40.0 MB", self.window.agy_progress_label.text())

        # 3. Complete & binary found -> hides Install/Cancel, reveals Login button and completion notice
        self.window.completed("status", self._status(binary_found=True, install={
            "state": "complete",
            "stage": "complete",
            "version": "1.9.0",
            "percent": 100,
        }))
        self.assertTrue(self.window.agy_install_button.isHidden())
        self.assertTrue(self.window.agy_cancel_install_button.isHidden())
        self.assertFalse(self.window.agy_login_button.isHidden())
        self.assertEqual(self.window.agy_progress_bar.value(), 100)
        self.assertIn("v1.9.0", self.window.agy_progress_label.text())

    def test_antigravity_settings_dialog_opens_and_retranslates(self):
        dialog = AntigravityDialog(self.store, self.window)
        try:
            self.assertTrue(dialog.binary_path_label.text())
            self.assertTrue(dialog.profile_mode_label.text())
            dialog.completed("agy-settings-get", {"binaryPath": "C:\\agy.exe", "profileMode": "isolated"})
            self.assertEqual(dialog.binary_path.text(), "C:\\agy.exe")
            self.assertEqual(dialog.profile_mode.currentData(), "isolated")
            self.assertTrue(dialog.save.isEnabled())
            dialog.retranslate("en")
            self.assertEqual(dialog.windowTitle(), "Antigravity settings")
        finally:
            dialog.close()


if __name__ == "__main__":
    unittest.main()
