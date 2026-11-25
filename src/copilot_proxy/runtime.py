from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, suppress
from copy import deepcopy
from dataclasses import asdict
import json
import logging
import os
import secrets
import socket
import time
from typing import Any

import aiohttp
from aiohttp import web

from .config import InstanceLock, ListenerSettings, ProxySettings, UserStore, atomic_write, read_project_config
from .network import CopilotClient, Network, UpstreamError
from .protocol import ProtocolError, ResponseStream, read_events, to_chat, to_responses

log = logging.getLogger(__name__)

CONTROL_UNAVAILABLE = "Cannot connect to Github Copilot Proxy. The service state is unknown."
CONTROL_LEGACY = "Control data belongs to the legacy Node version. Confirm that version has exited before restarting."
CONTROL_INVALID = "Service control data is invalid or incompatible. Check this installation's data directory."
CONTROL_CONNECTION_ERRORS = (CONTROL_UNAVAILABLE, CONTROL_LEGACY, CONTROL_INVALID)


def validate_control_record(control: Any) -> dict:
    if control is None:
        raise ConnectionError(CONTROL_UNAVAILABLE)
    if not isinstance(control, dict):
        raise ConnectionError(CONTROL_INVALID)
    if control.get("runtime") != "python":
        if control.get("runtime") == "node" or ("key" in control and "pid" in control and "port" not in control):
            raise ConnectionError(CONTROL_LEGACY)
        raise ConnectionError(CONTROL_INVALID)
    if (type(control.get("port")) is not int or not 1 <= control["port"] <= 65535
            or not isinstance(control.get("token"), str) or not control["token"]):
        raise ConnectionError(CONTROL_INVALID)
    return control


def safe_error(error: BaseException) -> str:
    if isinstance(error, (asyncio.CancelledError, TimeoutError)):
        return "Request cancelled, timed out or interrupted by a network change."
    if isinstance(error, aiohttp.ClientError):
        return "Network request failed. Check proxy, connectivity and certificates."
    if isinstance(error, json.JSONDecodeError):
        return "Invalid JSON data."
    return str(error) or type(error).__name__


class Runtime:
    def __init__(self, store: UserStore, *, client: CopilotClient | None = None, request_interval: float = 3):
        self.store = store
        self.listener = ListenerSettings.load(store.root)
        self.network = client.network if client else Network(self.configured_proxy(read_project_config(store.root)))
        self.client = client or CopilotClient(self.network)
        self.credentials = store.read("auth")
        self.state = "stopped" if self.credentials else "signed-out"
        self.message = ""
        self.runner = None
        self.catalog = None
        self.catalog_at = 0.0
        self.forced_at = 0.0
        self.catalog_lock = asyncio.Lock()
        self.lifecycle = asyncio.Lock()
        self.rate_lock = asyncio.Lock()
        self.next_request = 0.0
        self.request_interval = request_interval
        self.routes = set()
        self.generation = 0
        self.login_task = None
        self.login_state = "idle"
        self.device = None
        self.shutdown = asyncio.Event()
        self.active_requests: set[asyncio.Task] = set()

    def configured_proxy(self, document: dict) -> ProxySettings:
        return ProxySettings.parse(document["proxy"] if "proxy" in document else self.store.read("settings", {}).get("proxy", {}))

    def status(self) -> dict:
        return {"state": self.state, "message": self.message, "baseUrl": self.listener.base_url,
                "authenticated": bool(self.credentials), "processId": os.getpid(), "loginState": self.login_state,
                "username": self.credentials.get("username") if self.credentials else None,
                "deviceCode": self.device.user_code if self.device else None,
                "verificationUri": self.device.verification_uri if self.device else None}

    def invalidate(self):
        self.generation += 1
        self.catalog = None
        self.routes.clear()
        self.client.invalidate()

    async def models(self, refresh: bool = False) -> list[dict]:
        async with self.catalog_lock:
            if not refresh and self.catalog is not None and time.monotonic() - self.catalog_at < 300:
                return deepcopy(self.catalog)
            if not self.credentials:
                raise UpstreamError(503, "Sign in to GitHub first.")
            generation = self.generation
            models = await self.client.models(self.credentials["token"])
            if generation != self.generation:
                raise UpstreamError(503, "Network changed while refreshing models; retry.")
            overrides = {m["id"]: m for m in self.listener.models}
            self.catalog = [{**m, **deepcopy(overrides.get(m["id"], {}))} for m in models]
            self.catalog_at = time.monotonic()
            return deepcopy(self.catalog)

    async def _stop(self):
        self.state = "stopping"
        self.invalidate()
        await self.network.replace(self.network.settings)
        current = asyncio.current_task()
        pending = [task for task in self.active_requests if task is not current]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if self.runner:
            await self.runner.cleanup()
            self.runner = None
        self.state = "stopped" if self.credentials else "signed-out"

    async def _cancel_login(self):
        if self.login_task and not self.login_task.done():
            self.login_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.login_task
            self.login_state = "failed"

    async def start(self):
        async with self.lifecycle:
            return await self._start()

    async def _start(self, settings: ListenerSettings | None = None):
        if self.shutdown.is_set():
            raise RuntimeError("Service is shutting down.")
        if self.runner:
            return self.status()
        if settings is None:
            document = read_project_config(self.store.root)
            settings = ListenerSettings.from_data(document)
            proxy = self.configured_proxy(document)
            if proxy != self.network.settings:
                await self.network.replace(proxy)
                self.invalidate()
        if settings != self.listener:
            self.listener = settings
            self.invalidate()
        if not self.credentials:
            self.state = "signed-out"
            return self.status()
        self.state, self.message = "starting", ""
        runner = web.AppRunner(web.Application(client_max_size=16 * 1024 * 1024), access_log=None, shutdown_timeout=5)
        runner.app.router.add_route("*", "/{path:.*}", self.handle)
        try:
            await self.models()
            if not self.credentials.get("username"):
                username = await self._load_username(self.credentials["token"])
                if username:
                    self.credentials = {**self.credentials, "username": username}
            await runner.setup()
            for port in (self.listener.port, self.listener.bridge_port):
                await web.TCPSite(runner, self.listener.host, port).start()
            self.runner = runner
            self.state = "running"
        except Exception as error:
            await runner.cleanup()
            self.state, self.message = "error", safe_error(error)
            raise
        return self.status()

    def listener_settings(self):
        saved = ListenerSettings.load(self.store.root)
        return {"host": saved.host, "port": saved.port, "bridgePort": saved.bridge_port,
                "running": self.runner is not None, "baseUrl": self.listener.base_url}

    async def set_listener(self, host: str, port: int):
        async with self.lifecycle:
            if self.shutdown.is_set():
                raise RuntimeError("Service is shutting down.")
            path = self.store.root / "adapter-config.json"
            original = path.read_bytes() if path.exists() else None
            document = json.loads(original.decode("utf-8-sig")) if original is not None else {}
            if not isinstance(document, dict):
                raise ValueError("Listener configuration must be a JSON object.")
            updated = {**document, "host": host, "port": port}
            candidate = ListenerSettings.from_data(updated)
            previous = self.listener
            running = self.runner is not None
            content = (json.dumps(updated, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            atomic_write(path, content)
            if candidate == previous:
                return self.status()
            if not running:
                self.listener = candidate
                self.invalidate()
                return self.status()
            try:
                await self._stop()
                await self._start(candidate)
            except Exception as error:
                failures = []
                try:
                    if path.read_bytes() != content:
                        raise RuntimeError("Configuration changed externally; it was not overwritten during rollback.")
                    if original is None:
                        path.unlink()
                    else:
                        atomic_write(path, original)
                except (OSError, RuntimeError) as restore_error:
                    failures.append(safe_error(restore_error))
                self.listener = previous
                self.invalidate()
                try:
                    await self._start(previous)
                except Exception as restore_error:
                    failures.append(safe_error(restore_error))
                if failures:
                    self.message = "Listener update failed and rollback was incomplete: " + "; ".join(failures)
                else:
                    self.message = "Listener update failed; previous settings and service were restored. " + safe_error(error)
                log.error("%s", self.message)
                raise RuntimeError(self.message) from error
            return self.status()

    async def stop(self):
        async with self.lifecycle:
            await self._cancel_login()
            await self._stop()
            return self.status()

    async def begin_login(self):
        async with self.lifecycle:
            if self.shutdown.is_set():
                raise RuntimeError("Service is shutting down.")
            await self._cancel_login()
            await self._stop()
            self.device = await self.client.begin_login()
            self.state, self.login_state, self.message = "signing-in", "pending", ""
            self.login_task = asyncio.create_task(self._finish_login())
            return self.status()

    async def _finish_login(self):
        try:
            token = await self.client.complete_login(self.device)
            credentials = {"token": token}
            username = await self._load_username(token)
            if username:
                credentials["username"] = username
            self.store.write("auth", credentials)
            self.credentials = credentials
            self.invalidate()
            self.state, self.login_state = "stopped", "complete"
        except (Exception, asyncio.CancelledError) as error:
            self.login_state = "failed"
            self.state = "stopped" if self.credentials else "signed-out"
            self.message = safe_error(error)

    async def _load_username(self, token: str) -> str | None:
        try:
            async with asyncio.timeout(5):
                return await self.client.username(token)
        except (aiohttp.ClientError, TimeoutError, UpstreamError, ValueError) as error:
            log.warning("GitHub username unavailable; sign-in remains valid: %s", safe_error(error))
            return None

    async def logout(self):
        async with self.lifecycle:
            await self._cancel_login()
            await self._stop()
            self.store.delete("auth")
            self.credentials = self.device = None
            self.state, self.login_state, self.message = "signed-out", "idle", ""
            return self.status()

    async def set_proxy(self, settings: ProxySettings):
        async with self.lifecycle:
            if self.shutdown.is_set():
                raise RuntimeError("Service is shutting down.")
            document = read_project_config(self.store.root)
            document["proxy"] = asdict(settings)
            await self._cancel_login()
            old = self.network.settings
            await self.network.replace(settings)
            try:
                atomic_write(self.store.root / "adapter-config.json", (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
            except Exception:
                await self.network.replace(old)
                raise
            self.invalidate()
            self.message = ""
            if self.credentials:
                try:
                    await self.models(True)
                except Exception as error:
                    self.message = "Proxy applied; model refresh failed: " + safe_error(error)
            return self.status()

    async def test_proxy(self, settings: ProxySettings):
        network = Network(settings)
        client = CopilotClient(network)
        try:
            if self.credentials:
                models = await client.models(self.credentials["token"])
                return {"message": f"Connection and authorization OK; {len(models)} chat models."}
            await client.json("GET", "https://api.github.com", headers={"user-agent": "Github-Copilot-Proxy"})
            return {"message": "GitHub connection OK. Sign in to verify Copilot access."}
        finally:
            await network.close()

    async def execute(self, request: dict) -> Any:
        if self.shutdown.is_set():
            raise ValueError("Service is shutting down.")
        command = request.get("command")
        if command == "status":
            return self.status()
        if command == "start":
            return await self.start()
        if command in ("stop", "cancel-login"):
            return await self.stop()
        if command == "login":
            return await self.begin_login()
        if command == "logout":
            return await self.logout()
        if command == "shutdown":
            await self.stop()
            self.shutdown.set()
            return self.status()
        if command == "models":
            return await self.models(True)
        if command == "proxy-get":
            return self.network.settings.public()
        if command == "listener-get":
            return self.listener_settings()
        if command == "listener-set":
            settings = request.get("listener")
            if not isinstance(settings, dict) or set(settings) != {"host", "port"}:
                raise ValueError("Listener settings must contain only host and port.")
            return await self.set_listener(settings["host"], settings["port"])
        if command in ("proxy-set", "proxy-test"):
            proxy = dict(request["proxy"])
            if request.get("keepPassword"):
                proxy["password"] = self.network.settings.password
            settings = ProxySettings.parse(proxy)
            return await (self.set_proxy(settings) if command == "proxy-set" else self.test_proxy(settings))
        raise ValueError("Unknown control command.")

    async def handle(self, request: web.Request) -> web.StreamResponse:
        task = asyncio.current_task()
        self.active_requests.add(task)
        stream = None
        try:
            if request.method == "GET" and request.path == "/v1/models":
                models = await self.models()
                return web.json_response({"object": "list", "data": [{"id": m["id"], "object": "model", "owned_by": "GitHub Copilot"} for m in models]})
            port = request.transport.get_extra_info("sockname")[1]
            raw = request.path == "/v1/responses" and port == self.listener.bridge_port
            if request.method != "POST" or (request.path != "/v1/chat/completions" and not raw):
                return web.json_response({"error": {"message": "Endpoint not found"}}, status=404)
            payload = await request.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("model"), str):
                raise ProtocolError("A model id and JSON object are required.")
            model = next((m for m in await self.models() if m["id"] == payload["model"]), None)
            if model is None and time.monotonic() - self.forced_at >= 10:
                self.forced_at = time.monotonic()
                model = next((m for m in await self.models(True) if m["id"] == payload["model"]), None)
            if model is None:
                raise ProtocolError("Model is not in the official Copilot catalog.")
            endpoints = model.get("supported_endpoints", [])
            responses = raw or "/responses" in endpoints or (not endpoints and model["id"] in self.routes)
            async with self.rate_lock:
                await asyncio.sleep(max(0, self.next_request - time.monotonic()))
                self.next_request = time.monotonic() + self.request_interval
            if not self.credentials:
                raise UpstreamError(503, "Signed out.")
            async with AsyncExitStack() as stack:
                upstream = await stack.enter_async_context(self.client.send(self.credentials["token"], "/responses" if responses else "/chat/completions",
                                                                            to_responses(payload) if responses and not raw else payload))
                if upstream.status == 400 and not responses and not endpoints:
                    error = await upstream.text()
                    if "unsupported_api_for_model" in error or "not accessible via the /chat/completions" in error:
                        responses = True
                        self.routes.add(model["id"])
                        upstream = await stack.enter_async_context(self.client.send(self.credentials["token"], "/responses", to_responses(payload)))
                if upstream.status >= 300:
                    raise UpstreamError(upstream.status, f"Copilot returned HTTP {upstream.status}.")
                if not payload.get("stream"):
                    result = await upstream.json(content_type=None)
                    return web.json_response(to_chat(result, model["id"]) if responses and not raw else result)
                stream = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
                await stream.prepare(request)
                converter = ResponseStream(model["id"])
                if responses and not raw:
                    await self.write_event(stream, converter.chunk({"role": "assistant", "content": ""}))
                done = False
                async for data in read_events(upstream.content):
                    if data == "[DONE]":
                        done = True
                        break
                    if responses and not raw:
                        chunk = converter.convert(json.loads(data))
                        if chunk is not None:
                            await self.write_event(stream, chunk)
                    else:
                        if raw and json.loads(data).get("type") in ("response.completed", "response.incomplete"):
                            done = True
                        await self.write_event(stream, data)
                if not (converter.completed if responses and not raw else done):
                    raise RuntimeError("Upstream stream ended without a completed response.")
                await self.write_event(stream, "[DONE]")
                return stream
        except asyncio.CancelledError:
            raise
        except Exception as error:
            failure = {"error": {"message": safe_error(error), "type": "copilot_adapter_error"}}
            log.warning("API request failed: %s", safe_error(error))
            if stream is not None and stream.prepared:
                try:
                    await self.write_event(stream, failure)
                    await self.write_event(stream, "[DONE]")
                except (ConnectionResetError, aiohttp.ClientConnectionError):
                    log.info("Client disconnected before the stream error could be delivered.")
                return stream
            status = error.status if isinstance(error, (UpstreamError, web.HTTPException)) else 400 if isinstance(error, (ProtocolError, json.JSONDecodeError, KeyError, TypeError)) else 502
            return web.json_response(failure, status=status)
        finally:
            self.active_requests.discard(task)

    @staticmethod
    async def write_event(stream, value):
        data = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        await stream.write(f"data: {data}\n\n".encode())

    async def close(self):
        await self.stop()
        await self.network.close()


class ControlServer:
    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        self.key = secrets.token_hex(32)
        self.server = None
        self.connections = set()

    async def start(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0, limit=65536)
        self.runtime.store.write("control", {"port": self.server.sockets[0].getsockname()[1], "token": self.key, "pid": os.getpid(), "runtime": "python"})

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.connections.add(task)
        try:
            async with asyncio.timeout(120):
                raw = await asyncio.wait_for(reader.readline(), 10)
                if len(raw) > 65536:
                    raise ValueError("Control request too large.")
                request = json.loads(raw)
                supplied = request.pop("token", "")
                if not isinstance(supplied, str) or not secrets.compare_digest(supplied, self.key):
                    raise ValueError("Unauthorized control request.")
                result = await self.runtime.execute(request)
                response = {"ok": True, "result": result}
        except Exception as error:
            response = {"ok": False, "error": safe_error(error)}
        except asyncio.CancelledError:
            writer.close()
            self.connections.discard(task)
            raise
        try:
            writer.write(json.dumps(response).encode() + b"\n")
            await writer.drain()
        except (ConnectionError, OSError):
            log.info("Control client disconnected.")
        finally:
            writer.close()
            self.connections.discard(task)

    async def close(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        if self.connections:
            await asyncio.gather(*list(self.connections), return_exceptions=True)
        self.runtime.store.delete("control")


def control_request(store: UserStore, request: dict, timeout: float = 125) -> Any:
    control = validate_control_record(store.read("control"))
    data = json.dumps({**request, "token": control["token"]}).encode() + b"\n"
    if len(data) > 65536:
        raise ValueError("Control request too large.")
    try:
        with socket.create_connection(("127.0.0.1", control["port"]), timeout=min(timeout, 2)) as sock:
            sock.settimeout(timeout)
            sock.sendall(data)
            with sock.makefile("rb") as source:
                reply = source.readline(65537)
            if len(reply) > 65536 or not reply.endswith(b"\n"):
                raise ValueError("Invalid control response.")
            response = json.loads(reply)
    except OSError as error:
        raise ConnectionError(CONTROL_UNAVAILABLE) from error
    if not response.get("ok"):
        raise ValueError(response.get("error", "Control operation failed."))
    return response["result"]


async def serve(store: UserStore):
    existing = store.read("control")
    if existing is not None:
        validate_control_record(existing)
    store.ensure()
    lock = InstanceLock(store)
    if not lock.acquire():
        raise RuntimeError("This installation is already running.")
    runtime = control = None
    try:
        runtime = Runtime(store)
        control = ControlServer(runtime)
        await control.start()
        try:
            await runtime.start()
        except Exception as error:
            log.error("Startup failed: %s", safe_error(error))
        await runtime.shutdown.wait()
    finally:
        try:
            if runtime:
                await runtime.close()
            if control:
                await control.close()
        finally:
            lock.close()
