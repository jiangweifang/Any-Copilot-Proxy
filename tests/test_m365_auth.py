import asyncio
import base64
from contextlib import asynccontextmanager
import json
from pathlib import Path
import tempfile
import time
import socket
import unittest
from unittest.mock import AsyncMock, patch

from copilot_proxy.config import UserStore
from copilot_proxy.m365_copilot.auth import M365Auth, M365Profile, menu_models, probe_models
from copilot_proxy.network import UpstreamError
from copilot_proxy.runtime import ControlServer, Runtime, control_request


def encoded(value):
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


def capture(*, encrypted=False, **claims):
    now = int(time.time())
    if encrypted:
        token = encoded({"alg": "dir", "enc": "A256GCM"}) + "..a.b.c"
    else:
        token = encoded({"alg": "RS256"}) + "." + encoded({
            "oid": "user-1", "tid": "tenant-1", "exp": now + 3600, "iat": now,
            "aud": "https://substrate.office.com", "upn": "test@example.invalid", **claims,
        }) + ".signature"
    return {"accessToken": token, "endpoint": "wss://substrate.office.com/m365Copilot/Chathub/user-1@tenant-1",
            "origin": "https://m365.cloud.microsoft", "userAgent": "TestBrowser/1", "capturedAt": int(time.time() * 1000)}


MENU = {"gptList": [{"clientPreferences": {"modelSelectorMetadata": {"availableModelSelectionOptions": [
    {"id": "Magic", "menuItemTitle": "Auto"}, {"menuItemTitle": "Family", "itemGroup": [{"id": "Chat", "menuItemTitle": "Quick"}]},
]}}}]}


class ProfileTests(unittest.TestCase):
    def test_jwt_and_encrypted_account_metadata_are_not_treated_as_verified(self):
        jwt = M365Profile.parse(capture())
        self.assertEqual(jwt.account, ("user-1", "tenant-1"))
        self.assertEqual(jwt.kind, "enterprise")
        self.assertNotIn(jwt.access_token, repr(jwt))
        current_page = M365Profile.parse({**capture(), "origin": "https://copilot.cloud.microsoft"})
        self.assertEqual(current_page.origin, "https://copilot.cloud.microsoft")
        personal = M365Profile.parse(capture(encrypted=True))
        self.assertEqual(personal.kind, "personal")
        self.assertIsNone(personal.expires_at)
        self.assertEqual(personal.scenario, "OfficeWebPremiumConsumerCopilot")
        missing = capture(encrypted=True)
        missing["endpoint"] = None
        with self.assertRaisesRegex(ValueError, "account address"):
            M365Profile.parse(missing)

    def test_rejects_graph_expired_identity_mismatch_and_untrusted_endpoints(self):
        cases = [
            capture(aud="https://graph.microsoft.com"),
            capture(exp=int(time.time()) - 1),
            capture(oid="another-user"),
            {**capture(), "endpoint": "wss://example.invalid/m365Copilot/Chathub/user-1@tenant-1"},
            {**capture(), "endpoint": capture()["endpoint"] + "?access_token=do-not-log"},
            {**capture(), "endpoint": "wss://substrate.office.com:444/m365Copilot/Chathub/user-1@tenant-1"},
            {**capture(), "userAgent": "bad\r\nAuthorization: secret"},
            {**capture(), "claims": {"oid": "spoofed"}},
            {**capture(), "accessToken": "x" * 32769},
        ]
        for value in cases:
            with self.subTest(case=cases.index(value)):
                with self.assertRaises(ValueError) as error:
                    M365Profile.parse(value)
                self.assertNotIn(value["accessToken"], str(error.exception))
                self.assertNotIn("do-not-log", str(error.exception))

    def test_menu_requires_account_options_and_keeps_real_auto_entry(self):
        self.assertEqual(menu_models(MENU), [{"id": "Chat", "name": "Quick"}, {"id": "Magic", "name": "Auto"}])
        for data in ({}, {"gptList": []}, {"gptList": [{"clientPreferences": "bad"}]}):
            with self.assertRaises(ValueError):
                menu_models(data)


class AuthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = UserStore(self.root, self.root / "user")
        self.store.ensure()
        self.auth = M365Auth(self.store)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_connect_needs_no_code_and_preserves_existing_authorization(self):
        value = capture()
        with self.assertRaisesRegex(ValueError, "disconnected"):
            self.auth.capture(value, "")
        connected = self.auth.connect()
        self.assertNotIn("pairingCode", connected)
        self.auth.capture(value, self.auth.link_id)
        self.assertEqual(self.auth.connect()["linkId"], connected["linkId"])
        self.assertEqual(self.auth.profile.access_token, value["accessToken"])
        public = json.dumps(self.auth.status())
        self.assertNotIn(value["accessToken"], public)
        self.assertFalse(self.auth.status()["chatReady"])
        other = capture(oid="user-2")
        other["endpoint"] = other["endpoint"].replace("user-1@", "user-2@")
        with self.assertRaisesRegex(ValueError, "different M365 account"):
            self.auth.capture(other, self.auth.link_id)
        restored = M365Auth(self.store)
        self.assertEqual(restored.profile.account, self.auth.profile.account)
        self.auth.disconnect()
        with self.assertRaisesRegex(ValueError, "disconnected"):
            self.auth.capture(value, "")
        self.assertIsNone(self.store.read("m365-auth"))

    async def test_old_token_cannot_replace_newer_capture(self):
        self.auth.connect()
        newer = capture(exp=int(time.time()) + 7200)
        self.auth.capture(newer, self.auth.link_id)
        with self.assertRaisesRegex(ValueError, "older"):
            self.auth.capture(capture(), self.auth.link_id)
        self.assertEqual(self.auth.profile.access_token, newer["accessToken"])

    async def test_rejection_persists_and_duplicate_does_not_make_it_usable(self):
        self.auth.connect()
        value = capture()
        self.auth.capture(value, self.auth.link_id)
        network = type("Network", (), {"generation": 0})()
        with patch("copilot_proxy.m365_copilot.auth.probe_models", new=AsyncMock(side_effect=UpstreamError(401, "M365 returned HTTP 401."))):
            with self.assertLogs("copilot_proxy.m365_copilot.auth", level="WARNING") as logged:
                with self.assertRaisesRegex(ValueError, "verification failed"):
                    await self.auth.verify(network)
        self.auth.capture(value, self.auth.link_id)
        self.assertEqual(self.auth.status()["state"], "needs-login")
        self.assertEqual(M365Auth(self.store).status()["state"], "needs-login")
        self.assertNotIn(value["accessToken"], str(logged.output))
        updated = capture(exp=int(time.time()) + 7200)
        self.auth.capture(updated, self.auth.link_id)
        self.assertEqual(self.auth.status()["state"], "captured")

    async def test_verification_does_not_restore_authorization_after_disconnect(self):
        self.auth.connect()
        self.auth.capture(capture(), self.auth.link_id)
        entered, finish = asyncio.Event(), asyncio.Event()
        async def pending(*args):
            entered.set()
            await finish.wait()
            return menu_models(MENU)
        with patch("copilot_proxy.m365_copilot.auth.probe_models", side_effect=pending):
            verification = asyncio.create_task(self.auth.verify(type("Network", (), {"generation": 0})()))
            await entered.wait()
            self.auth.disconnect()
            finish.set()
            with self.assertRaisesRegex(ValueError, "changed"):
                await verification
        self.assertEqual(self.auth.status()["state"], "disconnected")

    async def test_model_probe_has_fixed_destination_bounded_data_and_no_generation(self):
        class Content:
            async def iter_chunked(self, size):
                yield json.dumps(MENU).encode()
        class Response:
            status = 200
            content = Content()
        class Network:
            @asynccontextmanager
            async def request(inner, method, url, **kwargs):
                self.assertEqual(method, "GET")
                self.assertEqual(url, "https://substrate.office.com/m365Copilot/getgptlist")
                self.assertEqual(kwargs["headers"]["X-Scenario"], "OfficeWebIncludedCopilot")
                yield Response()
        self.assertEqual(len(await probe_models(Network(), M365Profile.parse(capture()))), 2)

    async def test_control_bridge_is_private_and_does_not_touch_github_login(self):
        self.store.write("auth", {"token": "github-test-secret", "username": "github-test-user"})
        runtime = Runtime(self.store)
        control = ControlServer(runtime)
        await control.start()
        try:
            connection = await asyncio.to_thread(control_request, self.store, {"command": "m365-connect"})
            state = await asyncio.to_thread(control_request, self.store, {"command": "m365-capture", "profile": capture(), "linkId": connection["linkId"]})
            self.assertEqual(state["state"], "captured")
            await asyncio.to_thread(control_request, self.store, {"command": "m365-disconnect"})
            self.assertEqual(runtime.credentials["token"], "github-test-secret")
            self.assertEqual(self.store.read("auth")["username"], "github-test-user")
        finally:
            await runtime.close()
            await control.close()

    async def test_old_browser_connection_cannot_publish_after_reconnecting(self):
        old_link = self.auth.connect()["linkId"]
        self.auth.disconnect()
        new_link = self.auth.connect()["linkId"]
        self.assertNotEqual(old_link, new_link)
        with self.assertRaisesRegex(ValueError, "outdated"):
            self.auth.capture(capture(), old_link)
        self.assertIsNone(self.auth.profile)
        self.auth.capture(capture(), new_link)

    async def test_sync_pause_preserves_credentials_and_blocks_new_captures(self):
        connection = self.auth.connect()
        original = capture()
        self.auth.capture(original, connection["linkId"])
        runtime = Runtime(self.store)
        try:
            await runtime.execute({"command": "m365-settings-set", "settings": {"enabled": False}})
            self.assertFalse(self.auth.status()["syncEnabled"])
            self.assertEqual(self.auth.status()["state"], "captured")
            with self.assertRaisesRegex(ValueError, "paused"):
                self.auth.capture(capture(exp=int(time.time()) + 7200), connection["linkId"])
            self.assertEqual(self.store.read("m365-auth")["profile"]["access_token"], original["accessToken"])
            await runtime.execute({"command": "m365-settings-set", "settings": {"enabled": True}})
            self.assertTrue(self.auth.status()["syncEnabled"])
            self.auth.capture(capture(exp=int(time.time()) + 7200), connection["linkId"])
        finally:
            await runtime.close()

    async def test_fixed_control_port_binds_on_start_and_extension_mismatch_is_rejected(self):
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            port = reserved.getsockname()[1]
        runtime = Runtime(self.store)
        control = ControlServer(runtime)
        try:
            await runtime.execute({"command": "m365-settings-set", "settings": {"controlPort": port}})
            await control.start()
            self.assertEqual(self.store.read("control")["port"], port)
            status = await asyncio.to_thread(control_request, self.store, {"command": "status"}, expected_port=port)
            self.assertFalse(status["authenticated"])
            with self.assertRaisesRegex(ValueError, "does not match"):
                await asyncio.to_thread(control_request, self.store, {"command": "status"}, expected_port=port % 65535 + 1)
            with socket.socket() as occupied:
                occupied.bind(("127.0.0.1", 0))
                with self.assertRaisesRegex(ValueError, "already in use"):
                    await runtime.set_m365_settings({"controlPort": occupied.getsockname()[1]})
            self.assertEqual(runtime.m365_settings()["controlPort"], port)
        finally:
            await control.close()
            await runtime.close()
