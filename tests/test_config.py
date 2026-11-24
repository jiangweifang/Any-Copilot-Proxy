import json
from pathlib import Path
import tempfile
import unittest

from copilot_proxy.config import ListenerSettings, ProxySettings, UserStore


class ConfigurationTests(unittest.TestCase):
    def test_listener_defaults_specific_addresses_and_invalid_values(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            self.assertEqual(ListenerSettings.load(root).host, "127.0.0.1")
            for host in ("127.0.0.1", "0.0.0.0", "192.168.1.3"):
                (root / "adapter-config.json").write_text(json.dumps({"host": host}))
                self.assertEqual(ListenerSettings.load(root).host, host)
            for config in ({"host": "localhost"}, {"host": "127.1"}, {"host": None}, {"port": False},
                           {"port": 0}, {"bridgePort": 65536}, {"port": 4141}, {"models": [None]}):
                (root / "adapter-config.json").write_text(json.dumps(config))
                with self.assertRaises((ValueError, TypeError)):
                    ListenerSettings.load(root)

    def test_proxy_password_is_not_returned_in_public_settings(self):
        value = ProxySettings.parse({"type": "socks5", "host": "127.0.0.1", "port": 1080, "password": "test-secret"})
        self.assertTrue(value.public()["hasPassword"])
        self.assertNotIn("password", value.public())
        for config in ({"type": "socks4"}, {"type": "http", "host": "http://bad", "port": 8080}, {"type": "http", "host": "localhost", "port": 0}):
            with self.assertRaises(ValueError):
                ProxySettings.parse(config)

    def test_invalid_user_json_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            store = UserStore(root, root / "user")
            store.ensure()
            file = store.directory / "settings.json"
            file.write_text("not-json")
            with self.assertRaises(ValueError):
                store.read("settings")
            self.assertEqual(file.read_text(), "not-json")
