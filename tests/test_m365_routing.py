import asyncio
import base64
from contextlib import asynccontextmanager
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

import aiohttp
from aiohttp import web

from copilot_proxy.config import ProxySettings, UserStore
from copilot_proxy.m365_copilot.auth import M365Auth
from copilot_proxy.m365_copilot.client import FrameDecoder, M365Client, RS
from copilot_proxy.m365_copilot.protocol import ChatInput, ToolStream
from copilot_proxy.network import Network, UpstreamError
from copilot_proxy.protocol import ProtocolError
from copilot_proxy.runtime import Runtime
from test_m365_auth import capture, MENU
from test_runtime import FakeClient


class FixtureNetwork(Network):
    def __init__(self, base):
        super().__init__(ProxySettings())
        self.base = base

    @asynccontextmanager
    async def request(self, method, url, **kwargs):
        path = "/models" if url.endswith("/getgptlist") else "/upload"
        async with super().request(method, self.base + path, **kwargs) as response:
            yield response

    @asynccontextmanager
    async def websocket(self, url, **kwargs):
        async with super().websocket(self.base.replace("http:", "ws:") + "/socket", **kwargs) as connection:
            yield connection


class ProtocolTests(unittest.TestCase):
    def test_large_context_is_not_blocked_by_the_old_128k_character_limit(self):
        text = "context " * 25000
        self.assertEqual(ChatInput.parse({"messages": [{"role": "user", "content": text}]}).prompt, text)
        with self.assertRaisesRegex(ProtocolError, "2 Mi-character"):
            ChatInput.parse({"messages": [{"role": "user", "content": "x" * (2 * 1024 * 1024 + 1)}]})

    def test_tool_split_frames_validate_schema_and_required_choice(self):
        payload = {"messages": [{"role": "user", "content": "Use echo."}], "tools": [{
            "type": "function", "function": {"name": "echo", "parameters": {
                "type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"], "additionalProperties": False}}}],
            "tool_choice": "required", "parallel_tool_calls": False}
        parser = ToolStream(ChatInput.parse(payload))
        deltas = []
        text = 'Checking. <tool_call>{"name":"echo","arguments":{"value":"hello"}}</tool_call>'
        for character in text:
            deltas.extend(parser.push(character))
        deltas.extend(parser.finish())
        self.assertEqual("".join(item.get("content", "") for item in deltas), "Checking. ")
        calls = [call for item in deltas for call in item.get("tool_calls", [])]
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]), {"value": "hello"})
        for response in ('<tool_call>{"name":"other","arguments":{}}</tool_call>',
                         '<tool_call>{"name":"echo","arguments":{"value":3}}</tool_call>',
                         '<tool_call>{"name":"echo","arguments":',
                         'No function requested.'):
            with self.subTest(response=response), self.assertRaises(UpstreamError):
                parser = ToolStream(ChatInput.parse(payload))
                parser.push(response)
                parser.finish()

    def test_roles_history_and_unsupported_fields_are_explicit(self):
        messages = [{"role": "system", "content": "Be concise."},
                    {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1", "type": "function",
                        "function": {"name": "echo", "arguments": '{"value":"one"}'}}]},
                    {"role": "tool", "tool_call_id": "call_1", "content": "one"}]
        request = ChatInput.parse({"messages": messages})
        self.assertIn('"tool_call_id": "call_1"', request.prompt)
        self.assertIn('"role": "system"', request.prompt)
        for extra in ({"n": 2}, {"stop": ["end"]}, {"response_format": {"type": "json_object"}}):
            with self.assertRaises(ProtocolError):
                ChatInput.parse({"messages": messages, **extra})

    def test_snapshots_and_deltas_do_not_duplicate_text_and_fail_on_revision(self):
        parser = FrameDecoder(False)
        first = parser.consume({"type": 1, "arguments": [{"writeAtCursor": "O"}]})
        second = parser.consume({"type": 1, "arguments": [{"messages": [{"author": "bot", "messageId": "1", "text": "OK"}]}]})
        third = parser.consume({"type": 2, "item": {"messages": [{"author": "bot", "messageId": "1", "text": "OK"}]}})
        self.assertEqual("".join(first + second + third), "OK")
        with self.assertRaisesRegex(UpstreamError, "revised"):
            parser.consume({"type": 1, "arguments": [{"messages": [{"author": "bot", "messageId": "1", "text": "Different"}]}]})


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = UserStore(self.root, self.root / "user")
        self.store.ensure()
        reserved = [socket.socket(), socket.socket()]
        for item in reserved:
            item.bind(("127.0.0.1", 0))
        self.ports = [item.getsockname()[1] for item in reserved]
        for item in reserved:
            item.close()
        (self.root / "adapter-config.json").write_text(json.dumps({"port": self.ports[0], "bridgePort": self.ports[1]}))
        auth = M365Auth(self.store)
        link = auth.connect()["linkId"]
        auth.capture(capture(), link)
        self.mode = "normal"
        self.requests = []
        self.uploads = []
        self.release = asyncio.Event()
        app = web.Application(client_max_size=16 * 1024 * 1024)
        app.router.add_get("/models", self.models_handler)
        app.router.add_post("/upload", self.upload_handler)
        app.router.add_get("/socket", self.socket_handler)
        self.fixture = web.AppRunner(app, access_log=None)
        await self.fixture.setup()
        site = web.TCPSite(self.fixture, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.m365 = M365Client(FixtureNetwork(f"http://127.0.0.1:{port}"))
        self.github = FakeClient()
        self.github.available = [{"id": "Chat"}]
        self.runtime = Runtime(self.store, client=self.github, m365_client=self.m365, request_interval=0)
        self.http = aiohttp.ClientSession()
        self.base = f"http://127.0.0.1:{self.ports[0]}/v1"

    async def asyncTearDown(self):
        self.release.set()
        await self.http.close()
        await self.runtime.close()
        await self.fixture.cleanup()
        self.temp.cleanup()

    async def models_handler(self, request):
        return web.json_response(MENU)

    async def upload_handler(self, request):
        self.uploads.append(await request.post())
        return web.json_response({"docId": "test-image"})

    async def socket_handler(self, request):
        if self.mode == "unauthorized":
            return web.Response(status=401)
        websocket = web.WebSocketResponse()
        await websocket.prepare(request)
        self.assertEqual(json.loads((await websocket.receive_str()).rstrip(RS)), {"protocol": "json", "version": 1})
        await websocket.send_str("{}" + RS)
        frames = [json.loads(part) for part in (await websocket.receive_str()).split(RS) if part]
        invocation = frames[0]
        self.assertEqual(frames[1]["target"], "Metrics")
        self.assertEqual(invocation["arguments"][0]["conversationId"], request.query["ConversationId"])
        self.requests.append(invocation["arguments"][0])
        if self.mode == "hold":
            await self.release.wait()
        if self.mode == "throttled":
            await websocket.send_str(json.dumps({"type": 2, "item": {"result": {"value": "Throttled", "errorCode": "PerUserThrottled"}}}) + RS)
        elif self.mode == "tool":
            text = '<tool_call>{"name":"echo","arguments":{"value":"hello"}}</tool_call>'
            for delta in (text[:5], text[5:20], text[20:]):
                await websocket.send_str(json.dumps({"type": 1, "arguments": [{"writeAtCursor": delta}]}) + RS)
            await websocket.send_str(json.dumps({"type": 3, "invocationId": "0"}) + RS)
        elif self.mode == "incomplete":
            await websocket.send_str(json.dumps({"type": 1, "arguments": [{"writeAtCursor": "partial"}]}) + RS)
        else:
            frame = json.dumps({"type": 1, "arguments": [{"writeAtCursor": "O"}]}) + RS
            await websocket.send_str(frame[:10])
            await websocket.send_str(frame[10:] + json.dumps({"type": 1, "arguments": [{
                "messages": [{"author": "bot", "messageId": "m", "text": "OK"}]}]}) + RS)
            await websocket.send_str(json.dumps({"type": 2, "item": {"result": {"value": "Success"},
                "messages": [{"author": "bot", "messageType": "EndOfRequest"}]}}) + RS)
        await websocket.close()
        return websocket

    async def test_m365_only_starts_and_namespaced_models_route_without_github(self):
        status = await self.runtime.start()
        self.assertEqual(status["state"], "running")
        self.assertTrue(status["authenticated"])
        self.assertFalse(status["githubAuthenticated"])
        async with self.http.get(self.base + "/models") as response:
            models = (await response.json())["data"]
            self.assertTrue(all(model["id"].startswith("m365/") for model in models))
            self.assertIn("m365/Chat@paid", [model["id"] for model in models])
        async with self.http.post(self.base + "/chat/completions", json={"model": "m365/Chat", "messages": [{"role": "user", "content": "Hello"}]}) as response:
            body = await response.json()
            self.assertEqual(response.status, 200, body)
            self.assertEqual(body["model"], "m365/Chat")
            self.assertEqual(body["choices"][0]["message"]["content"], "OK")
        self.assertFalse(self.github.requests)

    async def test_same_model_names_coexist_and_m365_logout_leaves_github_running(self):
        self.runtime.credentials = {"token": "test-github"}
        await self.runtime.start()
        for model in ("Chat", "m365/Chat"):
            async with self.http.post(self.base + "/chat/completions", json={"model": model, "messages": [{"role": "user", "content": "Hello"}]}) as response:
                self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(len(self.github.requests), 1)
        self.assertEqual(len(self.requests), 1)
        await self.runtime.execute({"command": "m365-disconnect"})
        self.assertEqual(self.runtime.state, "running")
        self.assertIsNotNone(self.runtime.runner)
        self.assertEqual([model["id"] for model in await self.runtime.models()], ["Chat"])

    async def test_github_logout_does_not_interrupt_an_active_m365_stream(self):
        self.runtime.credentials = {"token": "test-github"}
        await self.runtime.start()
        self.mode = "hold"
        response = await self.http.post(self.base + "/chat/completions",
            json={"model": "m365/Chat", "stream": True, "messages": [{"role": "user", "content": "Hello"}]})
        try:
            first = await asyncio.wait_for(response.content.readline(), 3)
            self.assertIn(b"assistant", first)
            await self.runtime.logout()
            self.assertEqual(self.runtime.state, "running")
            self.release.set()
            rest = await asyncio.wait_for(response.text(), 3)
            self.assertIn("[DONE]", rest)
            self.assertNotIn("copilot_adapter_error", rest)
        finally:
            response.close()

    async def test_logging_switch_is_persistent_and_does_not_interrupt_an_active_stream(self):
        await self.runtime.start()
        await self.runtime.set_logging(True)
        runner, generation = self.runtime.runner, self.runtime.m365_network.generation
        self.mode = "hold"
        response = await self.http.post(self.base + "/chat/completions",
            json={"model": "m365/Chat", "stream": True, "messages": [{"role": "user", "content": "Hello"}]})
        try:
            await asyncio.wait_for(response.content.readline(), 3)
            identity = response.headers["X-Request-ID"]
            status = await self.runtime.execute({"command": "logging-set", "enabled": False})
            self.assertFalse(status["logging"]["enabled"])
            self.assertFalse(status["logging"]["active"])
            self.assertFalse(json.loads((self.root / "adapter-config.json").read_text())["logging"]["enabled"])
            await self.runtime.execute({"command": "logging-set", "enabled": True})
            self.assertIs(self.runtime.runner, runner)
            self.assertEqual(self.runtime.m365_network.generation, generation)
            self.release.set()
            text = await asyncio.wait_for(response.text(), 3)
            self.assertIn("[DONE]", text)
            self.assertNotIn("copilot_adapter_error", text)
            self.assertIn(f"request={identity}", self.runtime.log_manager.path.read_text(encoding="utf-8"))
            with patch("copilot_proxy.runtime.atomic_write", side_effect=PermissionError("read only")):
                with self.assertRaises(PermissionError):
                    await self.runtime.set_logging(False)
            self.assertTrue(self.runtime.log_manager.enabled)
            self.assertTrue(json.loads((self.root / "adapter-config.json").read_text())["logging"]["enabled"])
        finally:
            response.close()

    async def test_stream_errors_are_explicit_and_401_rejects_only_m365(self):
        self.runtime.credentials = {"token": "test-github"}
        await self.runtime.start()
        for mode in ("throttled", "incomplete"):
            self.mode = mode
            async with self.http.post(self.base + "/chat/completions", json={
                "model": "m365/Chat", "messages": [{"role": "user", "content": "Hello"}], "stream": True}) as response:
                text = await response.text()
                self.assertIn("copilot_adapter_error", text)
                self.assertIn("[DONE]", text)
        self.mode = "unauthorized"
        async with self.http.post(self.base + "/chat/completions", json={
            "model": "m365/Chat", "messages": [{"role": "user", "content": "Hello"}]}) as response:
            self.assertEqual(response.status, 401, await response.text())
        self.assertTrue(self.runtime.credentials)
        self.assertEqual(self.runtime.m365_auth().status()["state"], "needs-login")

    async def test_tool_result_round_trip_and_image_upload_are_forwarded(self):
        await self.runtime.start()
        self.mode = "tool"
        request = {"model": "m365/Chat", "messages": [{"role": "user", "content": "Use echo."}],
                   "tools": [{"type": "function", "function": {"name": "echo", "parameters": {
                       "type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]}}}],
                   "tool_choice": "required"}
        async with self.http.post(self.base + "/chat/completions", json=request) as response:
            result = await response.json()
            self.assertEqual(response.status, 200, result)
            message = result["choices"][0]["message"]
            self.assertEqual(result["choices"][0]["finish_reason"], "tool_calls")
        self.mode = "normal"
        request["messages"].extend([message, {"role": "tool", "tool_call_id": message["tool_calls"][0]["id"], "content": "hello"}])
        request["tool_choice"] = "auto"
        async with self.http.post(self.base + "/chat/completions", json=request) as response:
            self.assertEqual(response.status, 200, await response.text())
        self.assertIn("tool_call_id", self.requests[-1]["message"]["text"])
        image = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"test-image").decode()
        async with self.http.post(self.base + "/chat/completions", json={
            "model": "m365/Chat", "messages": [{"role": "user", "content": [{"type": "text", "text": "Describe."},
                {"type": "image_url", "image_url": {"url": image}}]}]}) as response:
            self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(self.uploads[-1]["FileBase64"], image)
        self.assertEqual(self.requests[-1]["message"]["messageAnnotations"][0]["id"], "test-image")
        with self.assertRaises(ProtocolError):
            await self.m365.load_image("https://127.0.0.1/private.png")
