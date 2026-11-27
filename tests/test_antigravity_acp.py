from __future__ import annotations

import base64
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from copilot_proxy.antigravity import AntigravityAuth, AntigravityClient, build_subprocess_env, extract_models
from copilot_proxy.chat_bridge import collect_chat
from copilot_proxy.config import AntigravitySettings, ProxySettings, UserStore
from copilot_proxy.network import UpstreamError
from copilot_proxy.protocol import ProtocolError

FAKE_SERVER = Path(__file__).with_name("fake_agy_acp.py").resolve()


def fake_command_factory(_binary: str) -> list[str]:
    return [sys.executable, str(FAKE_SERVER)]


class AntigravityAcpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "project"
        self.data = Path(self.temp.name) / "data"
        self.root.mkdir()
        self.store = UserStore(self.root, self.data)
        self.store.ensure()
        self.settings = AntigravitySettings(enabled=True, binary_path=sys.executable, profile_mode="shared")
        self.proxy = ProxySettings()

    def tearDown(self):
        self.temp.cleanup()

    def test_build_subprocess_env_proxy_and_isolated_profile(self):
        proxy = ProxySettings("socks5", "127.0.0.1", 1080, "u:1", "p@2")
        env = build_subprocess_env(proxy, store=self.store, profile_mode="isolated")
        self.assertEqual(env["HTTPS_PROXY"], "socks5h://u%3A1:p%402@127.0.0.1:1080")
        self.assertEqual(env["HTTP_PROXY"], env["HTTPS_PROXY"])
        self.assertEqual(env["ALL_PROXY"], env["HTTPS_PROXY"])
        self.assertTrue(Path(env["ANTIGRAVITY_HOME"]).is_dir())

        direct_env = build_subprocess_env(ProxySettings("none"))
        self.assertNotIn("HTTPS_PROXY", direct_env)
        self.assertNotIn("HTTP_PROXY", direct_env)

    def test_extract_models_from_both_formats(self):
        m1 = extract_models({
            "models": {"availableModels": [{"modelId": "gemini-2.5-pro", "name": "Gemini 2.5 Pro"}]}
        })
        self.assertEqual(m1[0]["id"], "agy/gemini-2.5-pro")
        self.assertEqual(m1[0]["raw_id"], "gemini-2.5-pro")

        m2 = extract_models({
            "configOptions": [{
                "id": "model",
                "category": "model",
                "options": [{"value": "gemini-2.5-flash", "name": "Flash"}],
            }]
        })
        self.assertEqual(m2[0]["id"], "agy/gemini-2.5-flash")
        self.assertEqual(m2[0]["config_option_id"], "model")

    async def test_oauth_personal_authenticate_and_models(self):
        auth = AntigravityAuth(self.store, command_factory=fake_command_factory)
        result = await auth.authenticate(self.settings, self.proxy)
        self.assertEqual(result["method"], "oauth-personal")
        self.assertEqual(result["account"], "tester@example.com")
        self.assertTrue(auth.authenticated)
        self.assertEqual([m["id"] for m in result["models"]], ["agy/gemini-2.5-pro", "agy/gemini-2.5-flash"])

        client = AntigravityClient(self.store, command_factory=fake_command_factory)
        models = await client.models(self.settings, self.proxy)
        self.assertEqual(len(models), 2)
        self.assertEqual(models[0]["owned_by"], "Google Antigravity")

    async def test_chat_streaming_reasoning_and_non_streaming_collect(self):
        client = AntigravityClient(self.store, command_factory=fake_command_factory)
        model = {"id": "agy/gemini-2.5-flash", "raw_id": "gemini-2.5-flash"}
        payload = {"model": "agy/gemini-2.5-flash", "messages": [{"role": "user", "content": "hello"}], "stream": True}
        chunks = [c async for c in client.chat(self.settings, self.proxy, model, payload)]
        collected = collect_chat(chunks, channel="Antigravity")
        msg = collected["choices"][0]["message"]
        self.assertEqual(msg["reasoning_content"], "thinking...")
        self.assertIn("[gemini-2.5-flash]", msg["content"])
        self.assertIn("echo:hello", msg["content"])
        self.assertEqual(collected["choices"][0]["finish_reason"], "stop")

    async def test_gateway_denies_builtin_tool_permission_requests(self):
        client = AntigravityClient(self.store, command_factory=fake_command_factory)
        model = {"id": "agy/gemini-2.5-pro", "raw_id": "gemini-2.5-pro"}
        with patch.dict(os.environ, {"FAKE_AGY_MODE": "request_permission"}):
            chunks = [c async for c in client.chat(self.settings, self.proxy, model, {
                "model": "agy/gemini-2.5-pro",
                "messages": [{"role": "user", "content": "run a command"}],
            })]
        collected = collect_chat(chunks, channel="Antigravity")
        self.assertEqual(collected["choices"][0]["message"]["content"], "permission=cancelled")

    async def test_tool_call_text_bridge(self):
        client = AntigravityClient(self.store, command_factory=fake_command_factory)
        model = {"id": "agy/gemini-2.5-pro", "raw_id": "gemini-2.5-pro"}
        payload = {
            "model": "agy/gemini-2.5-pro",
            "messages": [{"role": "user", "content": "weather in Tokyo"}],
            "tools": [{
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
                },
            }],
        }
        with patch.dict(os.environ, {"FAKE_AGY_MODE": "tool_call"}):
            chunks = [c async for c in client.chat(self.settings, self.proxy, model, payload)]
        collected = collect_chat(chunks, channel="Antigravity")
        self.assertEqual(collected["choices"][0]["finish_reason"], "tool_calls")
        self.assertEqual(collected["choices"][0]["message"]["tool_calls"][0]["function"]["name"], "get_weather")

    async def test_image_block_and_unsupported_image_error(self):
        client = AntigravityClient(self.store, command_factory=fake_command_factory)
        model = {"id": "agy/gemini-2.5-pro", "raw_id": "gemini-2.5-pro"}
        png_b64 = base64.b64encode(b"\x89PNG\r\n\x1a\n1234").decode()
        payload = {
            "model": "agy/gemini-2.5-pro",
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe"},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{png_b64}"}},
                ],
            }],
        }
        chunks = [c async for c in client.chat(self.settings, self.proxy, model, payload)]
        collected = collect_chat(chunks, channel="Antigravity")
        self.assertIn("images=1", collected["choices"][0]["message"]["content"])

        with patch.dict(os.environ, {"FAKE_AGY_IMAGE": "0"}):
            with self.assertRaises(ProtocolError):
                _ = [c async for c in client.chat(self.settings, self.proxy, model, payload)]

    async def test_unauthenticated_raises_401(self):
        client = AntigravityClient(self.store, command_factory=fake_command_factory)
        with patch.dict(os.environ, {"FAKE_AGY_MODE": "unauth"}):
            with self.assertRaises(UpstreamError) as ctx:
                await client.models(self.settings, self.proxy)
        self.assertEqual(ctx.exception.status, 401)

    async def test_proxy_env_propagates_to_subprocess(self):
        client = AntigravityClient(self.store, command_factory=fake_command_factory)
        model = {"id": "agy/gemini-2.5-pro", "raw_id": "gemini-2.5-pro"}
        proxy = ProxySettings("http", "127.0.0.1", 7890)
        with patch.dict(os.environ, {"FAKE_AGY_MODE": "echo_proxy"}):
            chunks = [c async for c in client.chat(self.settings, proxy, model, {
                "model": "agy/gemini-2.5-pro",
                "messages": [{"role": "user", "content": "check proxy"}],
            })]
        collected = collect_chat(chunks, channel="Antigravity")
        self.assertEqual(collected["choices"][0]["message"]["content"], "proxy=http://127.0.0.1:7890")


if __name__ == "__main__":
    unittest.main()
