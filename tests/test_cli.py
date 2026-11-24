import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from copilot_proxy.cli import dispatch
from copilot_proxy.config import UserStore


class CliTests(unittest.TestCase):
    def test_logout_rejection_or_eof_does_not_contact_the_service(self):
        with tempfile.TemporaryDirectory() as directory:
            store = UserStore(Path(directory), Path(directory) / "user")
            for answer in ("", "n", EOFError()):
                with patch("builtins.input", side_effect=answer if isinstance(answer, Exception) else None, return_value=answer), \
                     patch("copilot_proxy.cli.control_request") as control, redirect_stdout(io.StringIO()) as output:
                    asyncio.run(dispatch("logout", store, {"command": "logout"}))
                control.assert_not_called()
                self.assertIn("Logout cancelled", output.getvalue())
            self.assertFalse(store.directory.exists())

    def test_confirmed_cli_logout_clears_only_local_auth(self):
        with tempfile.TemporaryDirectory() as directory:
            store = UserStore(Path(directory), Path(directory) / "user")
            store.ensure()
            store.write("auth", {"token": "test-token", "username": "test-user"})
            store.write("settings", {"proxy": {"type": "none"}})
            with patch("builtins.input", return_value="yes"), \
                 patch("copilot_proxy.cli.control_request", side_effect=ConnectionError()), redirect_stdout(io.StringIO()):
                asyncio.run(dispatch("logout", store, {"command": "logout"}))
            self.assertIsNone(store.read("auth"))
            self.assertEqual(store.read("settings"), {"proxy": {"type": "none"}})

    def test_headless_cli_does_not_import_qt(self):
        result = subprocess.run([sys.executable, "-c", "import copilot_proxy.cli, sys; assert not any(k.startswith('PySide6') for k in sys.modules)"],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_offline_status_exits_without_starting_a_service(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, "-m", "copilot_proxy.cli", "status", "--root", directory],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["state"], "signed-out")
            self.assertEqual(list(Path(directory).iterdir()), [])
import asyncio
from contextlib import redirect_stdout
import io
