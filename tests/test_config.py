import json
from pathlib import Path
import tempfile
import unittest

from copilot_proxy.config import ListenerSettings, LoggingSettings, M365Settings, ProxySettings, UserStore


class ConfigurationTests(unittest.TestCase):
    def test_logging_switch_requires_a_boolean(self):
        for value in ("false", 0, None):
            with self.assertRaises(ValueError):
                ListenerSettings.from_data({"logging": {"enabled": value}})
        self.assertFalse(LoggingSettings.from_data({"logging": {"enabled": False}}).enabled)

    def test_control_port_cannot_collide_with_api_ports_or_accept_invalid_types(self):
        for data in ({"m365": {"controlPort": 4140}}, {"m365": {"controlPort": True}},
                     {"m365": {"controlPort": 65536}}, {"m365": {"enabled": "false"}}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                ListenerSettings.from_data(data)
        settings = M365Settings.from_data({"m365": {"enabled": False, "controlPort": 43210}})
        self.assertFalse(settings.enabled)
        self.assertEqual(settings.control_port, 43210)

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
