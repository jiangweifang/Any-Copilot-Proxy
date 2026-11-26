import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from copilot_proxy.config import InstanceLock, UserStore
from copilot_proxy.m365_copilot.native_host import dispatch, read_message, register, run, unregister, write_message
from copilot_proxy.runtime import CONTROL_UNAVAILABLE
from test_m365_auth import capture


class NativeProtocolTests(unittest.TestCase):
    def test_frames_handle_short_reads_and_clean_eof(self):
        frame = io.BytesIO()
        write_message(frame, {"action": "status"})
        class ShortReader(io.BytesIO):
            def read(self, size=-1):
                return super().read(min(size, 2))
        source = ShortReader(frame.getvalue())
        self.assertEqual(read_message(source), {"action": "status"})
        self.assertIsNone(read_message(source))

    def test_bad_frames_are_rejected_without_echoing_content(self):
        cases = (b"\x01", struct.pack("=I", 32769), struct.pack("=I", 0), struct.pack("=I", 100) + b"secret",
                 struct.pack("=I", 1) + b"\xff", struct.pack("=I", 6) + b"secret", struct.pack("=I", 2) + b"[]")
        for raw in cases:
            with self.subTest(length=len(raw)):
                with self.assertRaises(ValueError) as error:
                    read_message(io.BytesIO(raw))
                self.assertNotIn("secret", str(error.exception))

    def test_only_m365_commands_are_forwarded(self):
        with patch("copilot_proxy.runtime.control_request", return_value={"state": "captured"}) as control:
            self.assertEqual(dispatch(None, {"action": "status"}), {"state": "captured", "serviceRunning": True})
            control.assert_called_once_with(None, {"command": "m365-status"}, timeout=15)
            for value in ({"action": "shutdown"}, {"action": "capture", "profile": {}, "command": "shutdown"}, {"action": []}):
                with self.assertRaises(ValueError):
                    dispatch(None, value)
            self.assertEqual(control.call_count, 1)

    def test_pipe_replies_are_framed_and_connection_error_is_redacted(self):
        source, destination = io.BytesIO(), io.BytesIO()
        write_message(source, {"action": "status"})
        source.seek(0)
        with patch("copilot_proxy.m365_copilot.native_host.dispatch", side_effect=ConnectionError("private-url?token=secret")):
            run(source, destination, None)
        destination.seek(0)
        reply = read_message(destination)
        self.assertFalse(reply["ok"])
        self.assertNotIn("secret", json.dumps(reply))

    def test_authentication_survives_without_a_running_gateway(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = UserStore(root, root / "user")
            store.ensure()
            with patch("copilot_proxy.runtime.control_request", side_effect=ConnectionError(CONTROL_UNAVAILABLE)):
                connected = dispatch(store, {"action": "connect"})
                value = capture()
                accepted = dispatch(store, {"action": "capture", "profile": value, "linkId": connected["linkId"]})
                self.assertFalse(accepted["serviceRunning"])
                restored = dispatch(store, {"action": "status"})
                self.assertEqual(restored["state"], "captured")
                self.assertEqual(restored["accountKind"], "enterprise")
                self.assertNotIn(value["accessToken"], json.dumps(restored))
                saved = store.read("m365-auth")
                saved["profile"]["expires_at"] = 1
                store.write("m365-auth", saved)
                self.assertEqual(dispatch(store, {"action": "status"})["state"], "needs-login")
                dispatch(store, {"action": "disconnect", "linkId": connected["linkId"]})
                self.assertIsNone(store.read("m365-auth"))

    def test_offline_capture_does_not_race_gateway_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = UserStore(root, root / "user")
            store.ensure()
            lock = InstanceLock(store)
            self.assertTrue(lock.acquire())
            try:
                with patch("copilot_proxy.runtime.control_request", side_effect=ConnectionError(CONTROL_UNAVAILABLE)):
                    with self.assertRaises(ConnectionError):
                        dispatch(store, {"action": "connect"})
                self.assertIsNone(store.read("m365-auth"))
            finally:
                lock.close()


class NativeRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = UserStore(self.root, self.root / "user")
        self.store.ensure()
        self.exe = self.root / "native.exe"
        self.exe.write_bytes(b"test")
        self.manifest = self.root / "edge" / "manifest.json"
        self.metadata = self.root / "native.json"
        self.patches = [
            patch("copilot_proxy.m365_copilot.native_host.host_executable", return_value=self.exe),
            patch("copilot_proxy.m365_copilot.native_host.metadata_path", return_value=self.metadata),
            patch("copilot_proxy.m365_copilot.native_host.manifest_path", return_value=self.manifest),
            patch("copilot_proxy.m365_copilot.native_host.registry_target", return_value=None),
            patch("copilot_proxy.m365_copilot.native_host.register_registry"),
            patch("copilot_proxy.m365_copilot.native_host.unregister_registry"),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def test_registration_is_owned_idempotent_and_cleanup_is_targeted(self):
        unrelated = self.root / "edge" / "other.json"
        self.assertTrue(register(self.store, "a" * 32)["registered"])
        self.assertTrue(register(self.store, "a" * 32)["registered"])
        unrelated.write_text("keep")
        manifest = json.loads(self.manifest.read_text())
        self.assertEqual(manifest["allowed_origins"], ["chrome-extension://" + "a" * 32 + "/"])
        self.assertFalse(unregister(self.store)["registered"])
        self.assertFalse(self.manifest.exists())
        self.assertFalse(self.metadata.exists())
        self.assertEqual(unrelated.read_text(), "keep")

    def test_registration_does_not_overwrite_foreign_or_modified_data(self):
        register(self.store, "a" * 32)
        self.metadata.write_text(json.dumps({"root": "another-installation", "extensionId": "b" * 32}))
        with self.assertRaisesRegex(ValueError, "another installation"):
            register(self.store, "a" * 32)
        with self.assertRaisesRegex(ValueError, "modified"):
            unregister(self.store)
        self.assertTrue(self.manifest.exists())
        self.assertIn("another-installation", self.metadata.read_text())

    def test_registration_failure_leaves_recoverable_ownership_record(self):
        with patch("copilot_proxy.m365_copilot.native_host.atomic_write", side_effect=PermissionError("read only")):
            with self.assertRaises(PermissionError):
                register(self.store, "a" * 32)
        self.assertIsNotNone(self.store.read("native-registration"))
        unregister(self.store)
        self.assertIsNone(self.store.read("native-registration"))

    def test_extension_id_is_validated_before_changes(self):
        for extension_id in ("", "z" * 32, "../other"):
            with self.assertRaises(ValueError):
                register(self.store, extension_id)
        self.assertFalse(self.metadata.exists())
