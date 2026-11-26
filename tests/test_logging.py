import logging
from pathlib import Path
import tempfile
import unittest

import aiohttp
from aiohttp import web

from copilot_proxy.config import UserStore
from copilot_proxy.logging_setup import API_FAILED, ROUTE_MODEL, ROUTE_PROVIDER, request_logging, response_request_id, runtime_logging


class LoggingTests(unittest.TestCase):
    def test_persistent_logging_redacts_credentials_and_releases_the_file(self):
        with tempfile.TemporaryDirectory() as directory:
            store = UserStore(Path(directory), Path(directory) / "user")
            store.ensure()
            logger = logging.getLogger("copilot_proxy.test")
            old_handlers = list(logging.getLogger("copilot_proxy").handlers)
            with runtime_logging(store) as manager:
                path = manager.path
                logger.warning('url=https://user:proxy-secret@example.invalid/ws?access_token=query-secret '
                               'Authorization: Bearer bearer-secret apiKey="api-secret" password=password-secret '
                               'jwt=eyJhbGciOiJSUzI1NiJ9.eyJleHAiOjEyMzR9.signature')
                content = path.read_text(encoding="utf-8")
                for secret in ("proxy-secret", "query-secret", "bearer-secret", "api-secret", "password-secret",
                               "eyJhbGciOiJSUzI1NiJ9"):
                    self.assertNotIn(secret, content)
                self.assertIn("[redacted", content)
            self.assertEqual(logging.getLogger("copilot_proxy").handlers, old_handlers)
            path.unlink()

    def test_rotation_keeps_a_bounded_number_of_files(self):
        with tempfile.TemporaryDirectory() as directory:
            store = UserStore(Path(directory), Path(directory) / "user")
            store.ensure()
            with runtime_logging(store) as manager:
                path = manager.path
                handler = logging.getLogger("copilot_proxy").handlers[-1]
                handler.maxBytes = 512
                for index in range(50):
                    logging.getLogger("copilot_proxy.test").info("event=%d %s", index, "x" * 100)
                files = list(path.parent.glob("gateway.log*"))
                self.assertEqual(len(files), 4)
                self.assertTrue(all(item.stat().st_size <= 512 for item in files))

    def test_disabled_start_does_not_create_log_files_and_toggle_preserves_old_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            store = UserStore(Path(directory), Path(directory) / "user")
            store.ensure()
            with runtime_logging(store, False) as manager:
                self.assertFalse(manager.path.exists())
                manager.set_enabled(True)
                logging.getLogger("copilot_proxy.test").info("before-toggle")
                manager.set_enabled(False)
                saved = manager.path.read_bytes()
                logging.getLogger("copilot_proxy.test").warning("not-in-file-while-disabled")
                self.assertEqual(manager.path.read_bytes(), saved)
                manager.set_enabled(True)
                logging.getLogger("copilot_proxy.test").info("after-toggle")
                content = manager.path.read_text(encoding="utf-8")
                self.assertIn("before-toggle", content)
                self.assertIn("after-toggle", content)
                self.assertNotIn("not-in-file-while-disabled", content)


class RequestLoggingTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_errors_have_correlation_ids_and_never_log_request_content(self):
        with tempfile.TemporaryDirectory() as directory:
            store = UserStore(Path(directory), Path(directory) / "user")
            store.ensure()

            async def handle(request):
                await request.json()
                request[ROUTE_PROVIDER] = "m365"
                request[ROUTE_MODEL] = "m365/test-model"
                stream = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
                await stream.prepare(request)
                request[API_FAILED] = True
                logging.getLogger("copilot_proxy.test").warning("Simulated upstream failure.")
                await stream.write(b'data: {"error":{"message":"test failure"}}\n\ndata: [DONE]\n\n')
                return stream

            app = web.Application(middlewares=[request_logging])
            app.on_response_prepare.append(response_request_id)
            app.router.add_post("/v1/chat/completions", handle)
            runner = web.AppRunner(app, access_log=None)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            port = site._server.sockets[0].getsockname()[1]
            try:
                with runtime_logging(store) as manager:
                    path = manager.path
                    async with aiohttp.ClientSession() as session:
                        async with session.post(f"http://127.0.0.1:{port}/v1/chat/completions?access_token=query-secret",
                                                headers={"Authorization": "Bearer header-secret"},
                                                json={"messages": [{"role": "user", "content": "private-chat-content"}]}) as response:
                            self.assertEqual(response.status, 200)
                            identity = response.headers["X-Request-ID"]
                            self.assertEqual(len(identity), 32)
                            await response.text()
                    content = path.read_text(encoding="utf-8")
                    self.assertIn(f"request={identity}", content)
                    self.assertIn("provider=m365 model=m365/test-model status=200 outcome=error", content)
                    self.assertIn("duration_ms=", content)
                    for secret in ("private-chat-content", "header-secret", "query-secret"):
                        self.assertNotIn(secret, content)
            finally:
                await runner.cleanup()
