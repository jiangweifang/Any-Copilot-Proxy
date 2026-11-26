import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import aiohttp
from aiohttp import web

from copilot_proxy.config import InstanceLock, ListenerSettings, ProxySettings, UserStore
from copilot_proxy.network import Network, UpstreamError
from copilot_proxy.github_copilot.client import CopilotClient, DeviceCode
from copilot_proxy.platforms import clean_installation, install_startup, remove_startup
from copilot_proxy.runtime import CONTROL_INVALID, CONTROL_LEGACY, CONTROL_UNAVAILABLE, ControlServer, Runtime, control_request


class Content:
    def __init__(self, text): self.lines = text.splitlines(keepends=True)
    def __aiter__(self): return self
    async def __anext__(self):
        if not self.lines: raise StopAsyncIteration
        return self.lines.pop(0).encode()


class Response:
    def __init__(self, data, status=200, stream=""):
        self.data, self.status = data, status
        self.content = Content(stream)
    async def json(self, **kwargs): return self.data
    async def text(self): return json.dumps(self.data)


class FakeClient:
    def __init__(self):
        self.network = Network(ProxySettings())
        self.requests = []
        self.available = [{"id": "chat"}, {"id": "responses", "supported_endpoints": ["/responses"]}, {"id": "discover"}]
        self.authorized = asyncio.Event()
    def invalidate(self): pass
    async def models(self, token): return self.available
    async def username(self, token): return "test-user"
    async def begin_login(self): return DeviceCode("private-device", "USER-CODE", "https://github.com/login/device", 60, 1)
    async def complete_login(self, code):
        await self.authorized.wait()
        return "private-token"
    @asynccontextmanager
    async def send(self, token, path, payload):
        self.requests.append((path, payload))
        if path == "/chat/completions" and payload["model"] == "discover":
            yield Response({"error": "unsupported_api_for_model"}, 400)
            return
        text = 'data: {"type":"response.output_text.delta","delta":"OK"}\n\n'
        if payload.get("temperature") != 0:
            text += 'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'
        yield Response({"id": "r", "status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "OK"}]}],
                        "choices": [{"message": {"content": "OK"}}]}, stream=text)


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        reserved = [socket.socket(), socket.socket()]
        for sock in reserved: sock.bind(("127.0.0.1", 0))
        self.ports = [sock.getsockname()[1] for sock in reserved]
        for sock in reserved: sock.close()
        (self.root / "adapter-config.json").write_text(json.dumps({"port": self.ports[0], "bridgePort": self.ports[1]}))
        self.store = UserStore(self.root, self.root / "user")
        self.store.ensure()
        self.client = FakeClient()
        self.runtime = Runtime(self.store, client=self.client, request_interval=0)
        self.http = aiohttp.ClientSession()
        self.base = f"http://127.0.0.1:{self.ports[0]}/v1"

    async def asyncTearDown(self):
        await self.http.close()
        await self.runtime.close()
        self.temp.cleanup()

    async def test_api_catalog_routing_fallback_and_stream_errors(self):
        self.runtime.credentials = {"token": "private"}
        await self.runtime.start()
        async with self.http.get(self.base + "/models", headers={"Origin": "https://example.com"}) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(len((await response.json())["data"]), 3)
        for model in ("chat", "responses", "discover", "discover"):
            async with self.http.post(self.base + "/chat/completions", json={"model": model, "messages": [{"role": "user", "content": "Hi"}]}) as response:
                self.assertEqual(response.status, 200, await response.text())
        self.assertEqual([path for path, _ in self.client.requests], ["/chat/completions", "/responses", "/chat/completions", "/responses", "/responses"])
        for temperature, expected in ((1, '"finish_reason": "stop"'), (0, "copilot_adapter_error")):
            async with self.http.post(self.base + "/chat/completions", json={"model": "responses", "messages": [{"role": "user", "content": "Hi"}], "stream": True, "temperature": temperature}) as response:
                data = await response.text()
                self.assertIn(expected, data)
                self.assertIn("[DONE]", data)
        async with self.http.post(self.base + "/chat/completions", json={"model": "absent"}) as response:
            self.assertEqual(response.status, 400)

    async def test_login_is_bounded_cancellable_and_logout_preserves_proxy(self):
        await self.runtime.begin_login()
        self.client.authorized.set()
        await self.runtime.login_task
        self.assertEqual(self.runtime.login_state, "complete")
        self.assertIsNone(self.runtime.runner)
        self.assertEqual(self.store.read("auth")["token"], "private-token")
        self.assertEqual(self.store.read("auth")["username"], "test-user")
        self.assertEqual(self.runtime.status()["username"], "test-user")
        self.assertNotIn("private-token", json.dumps(self.runtime.status()))
        self.client.authorized.clear()
        await self.runtime.begin_login()
        await self.runtime.logout()
        self.assertIsNone(self.store.read("auth"))
        self.assertEqual(self.runtime.state, "signed-out")
        self.assertIsNone(self.runtime.status()["username"])
        self.assertIsNone(self.runtime.runner)

    async def test_username_failure_does_not_fail_login(self):
        with patch.object(self.client, "username", new=AsyncMock(side_effect=UpstreamError(403, "Profile unavailable"))):
            with self.assertLogs("copilot_proxy.runtime", level="WARNING") as logged:
                await self.runtime.begin_login()
                self.client.authorized.set()
                await self.runtime.login_task
        self.assertEqual(self.runtime.login_state, "complete")
        self.assertTrue(self.runtime.status()["authenticated"])
        self.assertIsNone(self.runtime.status()["username"])
        self.assertEqual(self.store.read("auth"), {"token": "private-token"})
        self.assertIn("username unavailable", logged.output[0])

    async def test_logout_cancels_pending_username_lookup(self):
        entered = asyncio.Event()
        async def pending_profile(token):
            entered.set()
            await asyncio.Event().wait()
        with patch.object(self.client, "username", new=pending_profile):
            await self.runtime.begin_login()
            self.client.authorized.set()
            await asyncio.wait_for(entered.wait(), timeout=1)
            await self.runtime.logout()
        self.assertFalse(self.runtime.status()["authenticated"])
        self.assertIsNone(self.store.read("auth"))
        self.assertIsNone(self.runtime.status()["username"])

    async def test_existing_login_loads_username_once_on_start(self):
        self.runtime.credentials = {"token": "private-token"}
        with patch.object(self.client, "username", new=AsyncMock(return_value="restored-user")) as lookup:
            await self.runtime.start()
            await self.runtime.start()
        lookup.assert_awaited_once_with("private-token")
        self.assertEqual(self.runtime.status()["username"], "restored-user")

    async def test_github_username_uses_authenticated_user_endpoint(self):
        client = CopilotClient(Network(ProxySettings()))
        with patch.object(client, "json", new=AsyncMock(return_value={"login": "test-user"})) as fetch:
            self.assertEqual(await client.username("test-token"), "test-user")
            self.assertEqual(fetch.call_args.args, ("GET", "https://api.github.com/user"))
            self.assertEqual(fetch.call_args.kwargs["headers"]["authorization"], "token test-token")
        with patch.object(client, "json", new=AsyncMock(return_value={"name": "Display name only"})):
            with self.assertRaisesRegex(ValueError, "username"):
                await client.username("test-token")
        await client.network.close()

    async def test_device_flow_pending_slow_down_and_denial(self):
        client = CopilotClient(Network(ProxySettings()))
        code = DeviceCode("device", "USER", "https://github.com/login/device", 30, 1)
        responses = [{"error": "authorization_pending"}, {"error": "slow_down"}, {"access_token": "test-token"},
                     {"token": "session", "endpoints": {"api": "https://api.githubcopilot.com"}, "refresh_in": 300}]
        with patch.object(client, "json", new=AsyncMock(side_effect=responses)), patch("copilot_proxy.github_copilot.client.asyncio.sleep", new=AsyncMock()) as sleep:
            self.assertEqual(await client.complete_login(code), "test-token")
            self.assertEqual([call.args[0] for call in sleep.await_args_list], [1, 1, 6])
        with patch.object(client, "json", new=AsyncMock(return_value={"error": "access_denied"})), patch("copilot_proxy.github_copilot.client.asyncio.sleep", new=AsyncMock()):
            with self.assertRaisesRegex(ValueError, "denied"):
                await client.complete_login(code)
        await client.network.close()

    async def test_management_authentication_settings_and_single_instance(self):
        control = ControlServer(self.runtime)
        await control.start()
        try:
            status = await asyncio.to_thread(control_request, self.store, {"command": "status"})
            self.assertEqual(status["state"], "signed-out")
            config = self.store.read("control")
            reader, writer = await asyncio.open_connection("127.0.0.1", config["port"])
            writer.write(b'{"token":"wrong","command":"logout"}\n')
            await writer.drain()
            self.assertFalse(json.loads(await reader.readline())["ok"])
            writer.close()
            await writer.wait_closed()
            await asyncio.to_thread(control_request, self.store, {"command": "proxy-set", "proxy": {"type": "http", "host": "localhost", "port": 9999, "password": "test-secret"}})
            public = await asyncio.to_thread(control_request, self.store, {"command": "proxy-get"})
            self.assertNotIn("password", public)
            self.assertTrue(public["hasPassword"])
            document = json.loads((self.root / "adapter-config.json").read_text())
            self.assertEqual(document["proxy"]["password"], "test-secret")
            self.assertEqual(document["port"], self.ports[0])
            first, second = InstanceLock(self.store), InstanceLock(self.store)
            try:
                self.assertTrue(first.acquire())
                self.assertFalse(second.acquire())
            finally: first.close(); second.close()
        finally: await control.close()

    async def test_control_connection_errors_do_not_claim_a_legacy_node_process_exists(self):
        with patch("copilot_proxy.runtime.socket.create_connection") as connect:
            for record, expected in (
                (None, CONTROL_UNAVAILABLE),
                ({"runtime": "unknown"}, CONTROL_INVALID),
                ({"runtime": "python", "port": False, "token": "test"}, CONTROL_INVALID),
                ({"key": "legacy-test-key", "pid": 123}, CONTROL_LEGACY),
            ):
                with self.subTest(record=record):
                    if record is None:
                        self.store.delete("control")
                    else:
                        self.store.write("control", record)
                    with self.assertRaises(ConnectionError) as raised:
                        control_request(self.store, {"command": "status"})
                    self.assertEqual(str(raised.exception), expected)
            connect.assert_not_called()
            self.store.write("control", {"runtime": "python", "port": 12345, "token": "test"})
            connect.side_effect = ConnectionRefusedError("No listener")
            with self.assertRaises(ConnectionError) as raised:
                control_request(self.store, {"command": "status"})
            self.assertEqual(str(raised.exception), CONTROL_UNAVAILABLE)
            self.assertNotIn("Node", str(raised.exception))

    async def test_network_switch_keeps_listeners_and_clears_catalog(self):
        self.runtime.credentials = {"token": "private"}
        await self.runtime.start()
        runner = self.runtime.runner
        self.client.available.append({"id": "new-model"})
        await self.runtime.set_proxy(ProxySettings("http", "localhost", 8080))
        self.assertIs(self.runtime.runner, runner)
        async with self.http.get(self.base + "/models") as response:
            self.assertIn("new-model", [model["id"] for model in (await response.json())["data"]])

    async def test_listener_update_preserves_other_config_and_does_not_start_stopped_service(self):
        path = self.root / "adapter-config.json"
        document = json.loads(path.read_text())
        document.update({"models": [{"id": "chat"}], "proxy": {"type": "none"}, "custom": {"keep": True}})
        path.write_text(json.dumps(document), encoding="utf-8")
        status = await self.runtime.execute({"command": "listener-set", "listener": {"host": "127.0.0.2", "port": self.ports[0]}})
        saved = json.loads(path.read_text())
        self.assertEqual(saved, {**document, "host": "127.0.0.2"})
        self.assertEqual(status["state"], "signed-out")
        self.assertIsNone(self.runtime.runner)
        self.assertEqual((await self.runtime.execute({"command": "listener-get"}))["bridgePort"], self.ports[1])

    async def test_running_listener_change_rebinds_both_ports_without_restarting_process(self):
        self.runtime.credentials = {"token": "private"}
        await self.runtime.start()
        process = self.runtime.status()["processId"]
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.2", 0))
            port = reservation.getsockname()[1]
        status = await self.runtime.set_listener("127.0.0.2", port)
        self.assertEqual(status["processId"], process)
        self.assertEqual(status["baseUrl"], f"http://127.0.0.2:{port}/v1")
        for target_port in (port, self.ports[1]):
            async with self.http.get(f"http://127.0.0.2:{target_port}/v1/models") as response:
                self.assertEqual(response.status, 200)
        with self.assertRaises(aiohttp.ClientConnectorError):
            await self.http.get(self.base + "/models")

    async def test_listener_bind_failure_restores_exact_config_and_old_service(self):
        self.runtime.credentials = {"token": "private"}
        await self.runtime.start()
        path = self.root / "adapter-config.json"
        original = path.read_bytes()
        occupied = await asyncio.start_server(lambda reader, writer: writer.close(), "127.0.0.1", 0)
        try:
            port = occupied.sockets[0].getsockname()[1]
            with self.assertLogs("copilot_proxy.runtime", level="ERROR"):
                with self.assertRaisesRegex(RuntimeError, "previous settings and service were restored"):
                    await self.runtime.set_listener("127.0.0.1", port)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(self.runtime.status()["baseUrl"], self.base)
            self.assertEqual(self.runtime.state, "running")
            async with self.http.get(self.base + "/models") as response:
                self.assertEqual(response.status, 200)
            self.assertTrue(occupied.is_serving())
        finally:
            occupied.close()
            await occupied.wait_closed()

    async def test_listener_validation_or_file_write_failure_does_not_stop_service(self):
        self.runtime.credentials = {"token": "private"}
        await self.runtime.start()
        runner = self.runtime.runner
        with self.assertRaises(ValueError):
            await self.runtime.set_listener("127.0.0.1", self.ports[1])
        with patch("copilot_proxy.runtime.atomic_write", side_effect=PermissionError("read-only configuration")):
            with self.assertRaises(PermissionError):
                await self.runtime.set_listener("127.0.0.2", self.ports[0])
        self.assertIs(self.runtime.runner, runner)
        async with self.http.get(self.base + "/models") as response:
            self.assertEqual(response.status, 200)

    async def test_cli_file_edits_are_loaded_on_next_service_start(self):
        self.runtime.credentials = {"token": "private"}
        await self.runtime.start()
        await self.runtime.stop()
        path = self.root / "adapter-config.json"
        document = json.loads(path.read_text())
        document.update({"host": "127.0.0.2", "proxy": {"type": "http", "host": "localhost", "port": 8080}})
        path.write_text(json.dumps(document))
        await self.runtime.start()
        self.assertEqual(self.runtime.listener.host, "127.0.0.2")
        self.assertEqual(self.runtime.network.settings.type, "http")
        async with self.http.get(f"http://127.0.0.2:{self.ports[0]}/v1/models") as response:
            self.assertEqual(response.status, 200)

    async def test_project_proxy_configuration_takes_precedence_over_legacy_settings(self):
        path = self.root / "adapter-config.json"
        document = json.loads(path.read_text())
        document["proxy"] = {"type": "none"}
        path.write_text(json.dumps(document))
        self.store.write("settings", {"proxy": {"type": "http", "host": "localhost", "port": 8080}})
        runtime = Runtime(self.store)
        try:
            self.assertEqual(runtime.network.settings.type, "none")
        finally:
            await runtime.close()

    async def test_http_proxy_and_socks_authentication_and_hot_disconnect(self):
        hits = []
        release = asyncio.Event()
        async def proxy_handler(request):
            hits.append(request.headers.get("Proxy-Authorization"))
            stream = web.StreamResponse()
            await stream.prepare(request)
            await stream.write(b"first")
            await release.wait()
            return stream
        app = web.Application()
        app.router.add_get("/{path:.*}", proxy_handler)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        network = Network(ProxySettings("http", "127.0.0.1", port, "user", "pass"))
        try:
            async with network.request("GET", "http://upstream.invalid/test") as response:
                self.assertEqual(await response.content.read(5), b"first")
                await network.replace(ProxySettings())
                with self.assertRaises((aiohttp.ClientError, RuntimeError)):
                    await response.content.read()
            self.assertEqual(hits, ["Basic dXNlcjpwYXNz"])
        finally:
            release.set()
            await network.close()
            await runner.cleanup()

        connect_headers = []
        async def reject_tunnel(reader, writer):
            try:
                connect_headers.append(await reader.readuntil(b"\r\n\r\n"))
                writer.write(b"HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
        tunnel = await asyncio.start_server(reject_tunnel, "127.0.0.1", 0)
        network = Network(ProxySettings("http", "127.0.0.1", tunnel.sockets[0].getsockname()[1], "user", "pass"))
        try:
            with self.assertRaises(aiohttp.ClientHttpProxyError):
                async with network.request("GET", "https://upstream.invalid/test"):
                    self.fail("Rejected proxy must not silently fall back to direct access")
            self.assertTrue(connect_headers[0].startswith(b"CONNECT upstream.invalid:443"))
            self.assertIn(b"Proxy-Authorization: Basic dXNlcjpwYXNz", connect_headers[0])
        finally:
            await network.close()
            tunnel.close()
            await tunnel.wait_closed()

        observed = []
        async def socks(reader, writer):
            try:
                greeting = await reader.readexactly(2)
                await reader.readexactly(greeting[1])
                writer.write(b"\x05\x02")
                await writer.drain()
                auth = await reader.readexactly(2)
                username = await reader.readexactly(auth[1])
                password_length = (await reader.readexactly(1))[0]
                password = await reader.readexactly(password_length)
                observed.append((username, password))
                writer.write(b"\x01\x00")
                await writer.drain()
                header = await reader.readexactly(4)
                self.assertEqual(header[3], 3)
                size = (await reader.readexactly(1))[0]
                observed.append(await reader.readexactly(size))
                await reader.readexactly(2)
                writer.write(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x50")
                await writer.drain()
                await reader.readuntil(b"\r\n\r\n")
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK")
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
        server = await asyncio.start_server(socks, "127.0.0.1", 0)
        network = Network(ProxySettings("socks5", "127.0.0.1", server.sockets[0].getsockname()[1], "user", "pass"))
        try:
            async with network.request("GET", "http://upstream.invalid/") as response:
                self.assertEqual(await response.text(), "OK")
            self.assertEqual(observed, [(b"user", b"pass"), b"upstream.invalid"])
        finally:
            await network.close()
            server.close()
            await server.wait_closed()

    async def test_startup_and_cleanup_are_scoped_to_owned_artifacts(self):
        fake_config = self.root / "xdg"
        self.store.write("auth", {"token": "private"})
        (self.root / "node_modules").mkdir()
        (self.root / "keep.txt").write_text("keep")
        with patch("copilot_proxy.platforms.sys.platform", "linux"), patch.dict(os.environ, {"XDG_CONFIG_HOME": str(fake_config)}):
            install_startup(self.store, True)
            location = Path(self.store.read("autostart")["location"])
            self.assertTrue(location.exists())
            message = clean_installation(self.store)
            self.assertIn("manually delete", message)
            self.assertFalse(location.exists())
            self.assertFalse(self.store.directory.exists())
            self.assertFalse((self.root / "node_modules").exists())
            self.assertTrue((self.root / "keep.txt").exists())
