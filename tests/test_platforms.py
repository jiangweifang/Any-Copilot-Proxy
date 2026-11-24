import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from copilot_proxy.config import UserStore
from copilot_proxy.platforms import install_startup, remove_startup, startup_enabled


class StartupTests(unittest.TestCase):
    def test_startup_state_checks_the_actual_file_not_only_saved_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = UserStore(root, root / "user")
            with patch("copilot_proxy.platforms.sys.platform", "linux"), \
                 patch.dict(os.environ, {"XDG_CONFIG_HOME": str(root / "xdg")}):
                self.assertFalse(startup_enabled(store))
                install_startup(store, True)
                self.assertTrue(startup_enabled(store))
                path = Path(store.read("autostart")["location"])
                path.unlink()
                self.assertIsNotNone(store.read("autostart"))
                self.assertFalse(startup_enabled(store))
                install_startup(store, True)
                remove_startup(store)
                self.assertFalse(startup_enabled(store))
                self.assertIsNone(store.read("autostart"))

    def test_changed_startup_file_is_reported_instead_of_overwritten_or_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = UserStore(root, root / "user")
            with patch("copilot_proxy.platforms.sys.platform", "linux"), \
                 patch.dict(os.environ, {"XDG_CONFIG_HOME": str(root / "xdg")}):
                install_startup(store, True)
                path = Path(store.read("autostart")["location"])
                path.write_text("modified externally", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "modified"):
                    startup_enabled(store)
                with self.assertRaisesRegex(ValueError, "modified"):
                    remove_startup(store)
                self.assertEqual(path.read_text(), "modified externally")

    def test_systemd_read_failure_is_not_reported_as_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = UserStore(root, root / "user")
            path = root / "example.service"
            path.write_text("unit", encoding="utf-8")
            store.ensure()
            store.write("autostart", {"kind": "systemd", "location": str(path), "content": "unit"})
            with patch("copilot_proxy.platforms.subprocess.run") as run:
                run.return_value.returncode = 1
                run.return_value.stdout = "disabled\n"
                self.assertFalse(startup_enabled(store))
                run.return_value.stdout = ""
                with self.assertRaisesRegex(RuntimeError, "Cannot read systemd"):
                    startup_enabled(store)
