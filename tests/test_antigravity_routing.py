from __future__ import annotations

import asyncio
import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest

import aiohttp

from copilot_proxy.antigravity import AntigravityAuth, AntigravityClient
from copilot_proxy.config import AntigravitySettings, UserStore
from copilot_proxy.runtime import Runtime

FAKE_SERVER = Path(__file__).with_name("fake_agy_acp.py").resolve()


def fake_command_factory(_binary: str) -> list[str]:
    return [sys.executable, str(FAKE_SERVER)]


def free_ports() -> tuple[int, int]:
    sockets = []
    ports = []
    try:
        for _ in range(2):
            s = socket.socket()
            s.bind(("127.0.0.1", 0))
            sockets.append(s)
            ports.append(s.getsockname()[1])
    finally:
        for s in sockets:
            s.close()
    return ports[0], ports[1]


class AntigravityRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "project"
        self.data = Path(self.temp.name) / "data"
        self.root.mkdir()
        self.port, self.bridge_port = free_ports()
        (self.root / "adapter-config.json").write_text(json.dumps({
            "host": "127.0.0.1",
            "port": self.port,
            "bridgePort": self.bridge_port,
            "antigravity": {
                "enabled": True,
                "binaryPath": sys.executable,
                "profileMode": "shared",
            },
        }), encoding="utf-8")
        self.store = UserStore(self.root, self.data)
        self.store.ensure()
        self.agy_auth = AntigravityAuth(self.store, command_factory=fake_command_factory)
        self.agy_client = AntigravityClient(self.store, command_factory=fake_command_factory)
        self.runtime = Runtime(
            self.store,
            agy_client=self.agy_client,
            agy_auth=self.agy_auth,
            request_interval=0,
        )

    async def asyncTearDown(self):
        await self.runtime.close()
        self.temp.cleanup()

    async def test_agy_only_login_start_models_chat_and_logout(self):
        # Before login, not authenticated
        self.assertFalse(self.runtime.status()["authenticated"])

        # Login via control command
        await self.runtime.execute({"command": "agy-login"})
        assert self.runtime.agy_login_task is not None
        await self.runtime.agy_login_task
        status = self.runtime.status()
        self.assertTrue(status["authenticated"])
        self.assertTrue(status["antigravity"]["authenticated"])
        self.assertEqual(status["antigravity"]["account"], "tester@example.com")

        # Start gateway with ONLY Antigravity signed in
        await self.runtime.start()
        self.assertEqual(self.runtime.state, "running")

        base = f"http://127.0.0.1:{self.port}"
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base}/v1/models") as resp:
                self.assertEqual(resp.status, 200)
                body = await resp.json()
                ids = [item["id"] for item in body["data"]]
                self.assertIn("agy/gemini-2.5-pro", ids)
                self.assertIn("agy/gemini-2.5-flash", ids)

            # Non-streaming chat completion
            async with session.post(f"{base}/v1/chat/completions", json={
                "model": "agy/gemini-2.5-flash",
                "messages": [{"role": "user", "content": "hi agy"}],
            }) as resp:
                self.assertEqual(resp.status, 200)
                body = await resp.json()
                self.assertEqual(body["model"], "agy/gemini-2.5-flash")
                self.assertIn("echo:hi agy", body["choices"][0]["message"]["content"])
                self.assertEqual(body["choices"][0]["message"]["reasoning_content"], "thinking...")

            # Streaming SSE chat completion
            async with session.post(f"{base}/v1/chat/completions", json={
                "model": "agy/gemini-2.5-pro",
                "messages": [{"role": "user", "content": "stream test"}],
                "stream": True,
            }) as resp:
                self.assertEqual(resp.status, 200)
                raw = await resp.text()
                self.assertIn("data: [DONE]", raw)
                self.assertIn("echo:stream test", raw)

            # /v1/responses on bridgePort rejects agy/ models cleanly
            async with session.post(f"http://127.0.0.1:{self.bridge_port}/v1/responses", json={
                "model": "agy/gemini-2.5-pro",
                "input": "hello",
            }) as resp:
                self.assertEqual(resp.status, 400)

        # Logout stops business listener when no other channel is signed in
        await self.runtime.execute({"command": "agy-logout"})
        self.assertEqual(self.runtime.state, "signed-out")
        self.assertIsNone(self.runtime.runner)

    def test_config_validation_rejects_invalid_antigravity_settings(self):
        with self.assertRaises(ValueError):
            AntigravitySettings.from_data({"antigravity": {"enabled": "yes"}})
        with self.assertRaises(ValueError):
            AntigravitySettings.from_data({"antigravity": {"profileMode": "unknown"}})
        with self.assertRaises(ValueError):
            AntigravitySettings.from_data({"antigravity": {"binaryPath": "bad\x00path"}})


if __name__ == "__main__":
    unittest.main()
