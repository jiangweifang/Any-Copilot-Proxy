from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, aclosing, suppress
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

from .antigravity import AntigravityAuth, AntigravityClient, AntigravityInstaller
from .chat_bridge import collect_chat
from .config import AntigravitySettings, InstanceLock, ListenerSettings, LoggingSettings, M365Settings, ProxySettings, UserStore, atomic_write, read_project_config
from .network import Network, UpstreamError
from .github_copilot.client import CopilotClient
from .m365_copilot.auth import M365Auth
from .m365_copilot.client import M365Client
from .logging_setup import API_FAILED, ROUTE_MODEL, ROUTE_PROVIDER, RuntimeLog, request_logging, response_request_id, runtime_logging
from .protocol import ProtocolError, ResponseStream, read_events, to_chat, to_responses

log = logging.getLogger(__name__)

CONTROL_UNAVAILABLE = "Cannot connect to Copilot Proxy. The service state is unknown."
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
    def __init__(self, store: UserStore, *, client: CopilotClient | None = None,
                 m365_client: M365Client | None = None, agy_client: AntigravityClient | None = None,
                 agy_auth: AntigravityAuth | None = None, agy_installer: AntigravityInstaller | None = None,
                 request_interval: float = 3, log_manager: RuntimeLog | None = None):
        self.store = store
        self.log_manager = log_manager or RuntimeLog(store)
        self.owns_log_manager = log_manager is None
        self.listener = ListenerSettings.load(store.root)
        self.network = client.network if client else Network(self.configured_proxy(read_project_config(store.root)))
        self.client = client or CopilotClient(self.network)
        self.m365_network = m365_client.network if m365_client else Network(self.network.settings)
        self.m365_client = m365_client or M365Client(self.m365_network)
        self.agy_client = agy_client or AntigravityClient(store)
        self.agy_installer = agy_installer or AntigravityInstaller()
        self.credentials = store.read("auth")
        self._m365: M365Auth | None = None
        self._agy: AntigravityAuth | None = agy_auth
        self.state = "stopped" if self.any_channel_ready() else "signed-out"
        self.message = ""
        self.runner = None
        self.catalog = None
        self.catalog_at = 0.0
        self.forced_at = 0.0
        self.catalog_lock = asyncio.Lock()
        self.m365_catalog = None
        self.m365_catalog_at = 0.0
        self.m365_catalog_key = None
        self.m365_catalog_lock = asyncio.Lock()
        self.m365_rate_lock = asyncio.Lock()
        self.m365_next_request = 0.0
        self.agy_catalog = None
        self.agy_catalog_at = 0.0
        self.agy_catalog_key = None
        self.agy_catalog_lock = asyncio.Lock()
        self.agy_rate_lock = asyncio.Lock()
        self.agy_next_request = 0.0
        self.agy_login_task = None
        self.agy_install_task = None
        self.lifecycle = asyncio.Lock()
        self.rate_lock = asyncio.Lock()
        self.next_request = 0.0
        self.request_interval = request_interval
        self.routes = set()
        self.generation = 0
        self.github_generation = 0
        self.login_task = None
        self.login_state = "idle"
        self.device = None
        self.shutdown = asyncio.Event()
        self.active_requests: set[asyncio.Task] = set()
        self.active_channels: dict[asyncio.Task, str] = {}
        self.running_requested = False

    def configured_proxy(self, document: dict) -> ProxySettings:
        return ProxySettings.parse(document["proxy"] if "proxy" in document else self.store.read("settings", {}).get("proxy", {}))

    def m365_auth(self) -> M365Auth:
        if self._m365 is None:
            self._m365 = M365Auth(self.store)
        return self._m365

    def m365_ready(self) -> bool:
        return self.m365_auth().status()["state"] == "captured"

    def agy_settings(self) -> AntigravitySettings:
        return AntigravitySettings.load(self.store.root)

    def agy_auth(self) -> AntigravityAuth:
        if self._agy is None:
            self._agy = AntigravityAuth(self.store)
        return self._agy

    def agy_ready(self) -> bool:
        return self.agy_auth().ready(self.agy_settings())

    def any_channel_ready(self) -> bool:
        return bool(self.credentials) or self.m365_ready() or self.agy_ready()

    def status(self) -> dict:
        m365 = self.m365_auth().status()
        m365["chatReady"] = m365["state"] == "captured" and self.runner is not None
        agy = self.agy_auth().status(self.agy_settings())
        agy["chatReady"] = agy["ready"] and self.runner is not None
        agy["install"] = self.agy_installer.status()
        return {"state": self.state, "message": self.message, "baseUrl": self.listener.base_url,
                "authenticated": self.any_channel_ready(), "githubAuthenticated": bool(self.credentials),
                "processId": os.getpid(), "loginState": self.login_state,
                "username": self.credentials.get("username") if self.credentials else None,
                "deviceCode": self.device.user_code if self.device else None,
                "verificationUri": self.device.verification_uri if self.device else None,
                "logFile": str(self.store.directory / "logs" / "gateway.log"),
                "logging": {"enabled": LoggingSettings.load(self.store.root).enabled, "active": self.log_manager.enabled},
                "m365": m365,
                "antigravity": agy}

    async def set_logging(self, enabled: bool) -> dict:
        if type(enabled) is not bool:
            raise ValueError("Logging settings require a boolean enabled field.")
        async with self.lifecycle:
            document = read_project_config(self.store.root)
            LoggingSettings.from_data(document)
            document["logging"] = {**document.get("logging", {}), "enabled": enabled}
            was_enabled = self.log_manager.enabled
            self.log_manager.set_enabled(enabled)
            try:
                atomic_write(self.store.root / "adapter-config.json", (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode())
            except OSError:
                self.log_manager.set_enabled(was_enabled)
                raise
            return self.status()

    def m365_settings(self) -> dict:
        settings = M365Settings.load(self.store.root)
        control = self.store.read("control", {})
        return {"enabled": settings.enabled, "controlPort": settings.control_port, "activeControlPort": control.get("port", 0)}

    async def set_m365_settings(self, changes: dict) -> dict:
        if not isinstance(changes, dict) or not changes or set(changes) - {"enabled", "controlPort"}:
            raise ValueError("M365 settings accept only enabled and controlPort.")
        async with self.lifecycle:
            document = read_project_config(self.store.root)
            document["m365"] = {**document.get("m365", {}), **changes}
            settings = M365Settings.from_data(document)
            active_port = self.store.read("control", {}).get("port", 0)
            if settings.control_port and settings.control_port != active_port:
                try:
                    with socket.socket() as probe:
                        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                        probe.bind(("127.0.0.1", settings.control_port))
                except OSError:
                    raise ValueError("The selected control port is already in use.") from None
            atomic_write(self.store.root / "adapter-config.json", (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode())
            log.info("M365 synchronization settings saved: enabled=%s control_port=%d", settings.enabled, settings.control_port)
            return self.m365_settings()

    def agy_settings_dict(self) -> dict:
        settings = self.agy_settings()
        return {"enabled": settings.enabled, "binaryPath": settings.binary_path, "profileMode": settings.profile_mode}

    async def set_agy_settings(self, changes: dict) -> dict:
        if not isinstance(changes, dict) or not changes or set(changes) - {"enabled", "binaryPath", "profileMode"}:
            raise ValueError("Antigravity settings accept only enabled, binaryPath and profileMode.")
        async with self.lifecycle:
            document = read_project_config(self.store.root)
            document["antigravity"] = {**document.get("antigravity", {}), **changes}
            settings = AntigravitySettings.from_data(document)
            atomic_write(self.store.root / "adapter-config.json", (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
            self.agy_catalog = None
            log.info("Antigravity settings saved: enabled=%s profile_mode=%s", settings.enabled, settings.profile_mode)
            if not settings.enabled:
                await self.cancel_channel("antigravity")
                if self.runner and not self.any_channel_ready():
                    await self._stop()
                elif not self.runner:
                    self.state = "stopped" if self.any_channel_ready() else "signed-out"
            else:
                if not self.runner and self.any_channel_ready():
                    self.state = "stopped"
                    if self.running_requested:
                        try:
                            await self._start()
                        except Exception as error:
                            log.warning("Antigravity enabled; service startup failed: %s", safe_error(error))
            return self.agy_settings_dict()

    def invalidate(self):
        self.generation += 1
        self.catalog = None
        self.routes.clear()
        self.client.invalidate()
        self.m365_catalog = None
        self.agy_catalog = None

    async def models(self, refresh: bool = False) -> list[dict]:
        models, errors = [], []
        providers = []
        if self.credentials:
            providers.append(("GitHub", self.github_models))
        if self.m365_ready():
            providers.append(("M365", self.m365_models))
        if self.agy_ready():
            providers.append(("Antigravity", self.agy_models))
        for name, fetch in providers:
            try:
                models.extend(await fetch(refresh))
            except (UpstreamError, aiohttp.ClientError, TimeoutError, ValueError) as error:
                log.warning("%s model catalog unavailable: %s", name, safe_error(error))
                errors.append(error)
        if not models:
            if errors:
                raise errors[0]
            raise UpstreamError(503, "Sign in to GitHub, M365 or Antigravity first.")
        return models

    async def github_models(self, refresh: bool = False) -> list[dict]:
        async with self.catalog_lock:
            if not refresh and self.catalog is not None and time.monotonic() - self.catalog_at < 300:
                return deepcopy(self.catalog)
            if not self.credentials:
                raise UpstreamError(503, "Sign in to GitHub first.")
            generation = self.generation, self.github_generation
            models = await self.client.models(self.credentials["token"])
            if generation != (self.generation, self.github_generation):
                raise UpstreamError(503, "Network changed while refreshing models; retry.")
            overrides = {m["id"]: m for m in self.listener.models}
            self.catalog = [{**m, **deepcopy(overrides.get(m["id"], {}))} for m in models]
            self.catalog_at = time.monotonic()
            log.info("GitHub model catalog refreshed: models=%d", len(self.catalog))
            return deepcopy(self.catalog)

    async def m365_models(self, refresh: bool = False) -> list[dict]:
        async with self.m365_catalog_lock:
            auth = self.m365_auth()
            if not self.m365_ready() or auth.profile is None:
                raise UpstreamError(401, "M365 authorization is missing or expired; refresh it in the browser.")
            key = auth.generation, self.generation
            if (not refresh and self.m365_catalog is not None and key == self.m365_catalog_key
                    and time.monotonic() - self.m365_catalog_at < 300):
                return deepcopy(self.m365_catalog)
            profile = auth.profile
            try:
                models = await self.m365_client.models(profile)
            except UpstreamError as error:
                self.reject_m365(error, profile)
                raise
            if key != (auth.generation, self.generation):
                raise UpstreamError(503, "M365 authorization or network changed while refreshing models; retry.")
            self.m365_catalog, self.m365_catalog_key = models, key
            self.m365_catalog_at = time.monotonic()
            log.info("M365 model catalog refreshed: models=%d", len(models))
            return deepcopy(models)

    def reject_m365(self, error: UpstreamError, profile):
        auth = self.m365_auth()
        if auth.profile != profile:
            return
        if error.status == 401:
            auth.save(auth.enabled, auth.profile, True)
            self.m365_catalog = None
        auth.last_error = safe_error(error)

    async def agy_models(self, refresh: bool = False) -> list[dict]:
        async with self.agy_catalog_lock:
            auth = self.agy_auth()
            settings = self.agy_settings()
            if not auth.ready(settings):
                raise UpstreamError(401, "Antigravity is not enabled or signed in.")
            key = auth.generation, self.generation, settings
            if (not refresh and self.agy_catalog is not None and key == self.agy_catalog_key
                    and time.monotonic() - self.agy_catalog_at < 300):
                return deepcopy(self.agy_catalog)
            try:
                models = await self.agy_client.models(settings, self.network.settings)
            except UpstreamError as error:
                if error.status == 401:
                    auth.mark_error(safe_error(error), clear_auth=True)
                    self.agy_catalog = None
                else:
                    auth.mark_error(safe_error(error))
                raise
            if key != (auth.generation, self.generation, self.agy_settings()):
                raise UpstreamError(503, "Antigravity settings or network changed while refreshing models; retry.")
            self.agy_catalog, self.agy_catalog_key = models, key
            self.agy_catalog_at = time.monotonic()
            log.info("Antigravity model catalog refreshed: models=%d", len(models))
            return deepcopy(models)

    async def cancel_channel(self, channel: str):
        current = asyncio.current_task()
        pending = [task for task, value in self.active_channels.items() if value == channel and task is not current]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _stop(self):
        log.info("Stopping business listeners; active_requests=%d", len(self.active_requests))
        self.state = "stopping"
        self.invalidate()
        await self.network.replace(self.network.settings)
        await self.m365_network.replace(self.m365_network.settings)
        current = asyncio.current_task()
        pending = [task for task in self.active_requests if task is not current]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if self.runner:
            await self.runner.cleanup()
            self.runner = None
        self.state = "stopped" if self.any_channel_ready() else "signed-out"

    async def _cancel_login(self):
        if self.login_task and not self.login_task.done():
            self.login_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.login_task
            self.login_state = "failed"

    async def _cancel_agy_login(self):
        if self.agy_login_task and not self.agy_login_task.done():
            self.agy_login_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.agy_login_task
            self.agy_auth().login_state = "failed"

    async def _cancel_agy_install(self):
        if self.agy_install_task and not self.agy_install_task.done():
            self.agy_install_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.agy_install_task

    async def start(self):
        async with self.lifecycle:
            self.running_requested = True
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
                await self.m365_network.replace(proxy)
                self.invalidate()
        if settings != self.listener:
            self.listener = settings
            self.invalidate()
        if not self.any_channel_ready():
            self.state = "signed-out"
            return self.status()
        self.state, self.message = "starting", ""
        runner = web.AppRunner(web.Application(client_max_size=16 * 1024 * 1024, middlewares=[request_logging]),
                               access_log=None, shutdown_timeout=5)
        runner.app.on_response_prepare.append(response_request_id)
        runner.app.router.add_route("*", "/{path:.*}", self.handle)
        try:
            await self.models()
            if self.credentials and not self.credentials.get("username"):
                username = await self._load_username(self.credentials["token"])
                if username:
                    self.credentials = {**self.credentials, "username": username}
            await runner.setup()
            for port in (self.listener.port, self.listener.bridge_port):
                await web.TCPSite(runner, self.listener.host, port).start()
            self.runner = runner
            self.state = "running"
            log.info("Business listeners ready: host=%s port=%d bridge_port=%d",
                     self.listener.host, self.listener.port, self.listener.bridge_port)
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
            self.running_requested = False
            await self._cancel_login()
            await self._cancel_agy_login()
            await self._cancel_agy_install()
            await self._stop()
            return self.status()

    async def begin_agy_install(self):
        async with self.lifecycle:
            if self.shutdown.is_set():
                raise RuntimeError("Service is shutting down.")
            if self.agy_install_task and not self.agy_install_task.done():
                return self.status()
            self.agy_install_task = asyncio.create_task(self._finish_agy_install())
            return self.status()

    async def _finish_agy_install(self):
        try:
            await self.agy_installer.install(self.network)
            self.agy_catalog = None
        except (Exception, asyncio.CancelledError) as error:
            if not isinstance(error, asyncio.CancelledError):
                log.warning("Antigravity CLI installation failed: %s", safe_error(error))

    async def begin_agy_login(self):
        async with self.lifecycle:
            if self.shutdown.is_set():
                raise RuntimeError("Service is shutting down.")
            await self._cancel_agy_login()
            auth = self.agy_auth()
            auth.login_state = "pending"
            auth.last_error = ""
            self.agy_login_task = asyncio.create_task(self._finish_agy_login())
            return self.status()

    async def _finish_agy_login(self):
        auth = self.agy_auth()
        try:
            settings = self.agy_settings()
            result = await auth.authenticate(settings, self.network.settings)
            if not settings.enabled:
                document = read_project_config(self.store.root)
                document["antigravity"] = {**document.get("antigravity", {}), "enabled": True}
                atomic_write(self.store.root / "adapter-config.json", (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
            self.agy_catalog = result.get("models") or None
            if self.agy_catalog:
                self.agy_catalog_key = (auth.generation, self.generation, self.agy_settings())
                self.agy_catalog_at = time.monotonic()
            auth.login_state = "complete"
            if not self.runner:
                self.state = "stopped"
                if self.running_requested:
                    try:
                        await self._start()
                    except Exception as error:
                        log.warning("Antigravity login completed; service startup failed: %s", safe_error(error))
        except (Exception, asyncio.CancelledError) as error:
            auth.login_state = "failed"
            auth.last_error = safe_error(error)

    async def agy_logout(self):
        async with self.lifecycle:
            await self._cancel_agy_login()
            keep_running = self.runner is not None and (bool(self.credentials) or self.m365_ready())
            if keep_running:
                await self.cancel_channel("antigravity")
                self.agy_catalog = None
            else:
                await self._stop()
            self.agy_auth().clear()
            log.info("Antigravity authorization removed.")
            self.agy_catalog = None
            self.state = "running" if keep_running else "stopped" if self.any_channel_ready() else "signed-out"
            return self.status()

    async def begin_login(self):
        async with self.lifecycle:
            if self.shutdown.is_set():
                raise RuntimeError("Service is shutting down.")
            await self._cancel_login()
            if self.runner and (self.m365_ready() or self.agy_ready()):
                await self.cancel_channel("github")
                await self.network.replace(self.network.settings)
                self.catalog = None
                self.client.invalidate()
                self.github_generation += 1
            else:
                await self._stop()
            self.device = await self.client.begin_login()
            self.state, self.login_state, self.message = "running" if self.runner else "signing-in", "pending", ""
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
            log.info("GitHub authorization completed.")
            self.credentials = credentials
            self.catalog = None
            self.routes.clear()
            self.client.invalidate()
            self.github_generation += 1
            self.state, self.login_state = "running" if self.runner else "stopped", "complete"
        except (Exception, asyncio.CancelledError) as error:
            self.login_state = "failed"
            self.state = "running" if self.runner else "stopped" if self.any_channel_ready() else "signed-out"
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
            keep_running = self.runner is not None and (self.m365_ready() or self.agy_ready())
            if keep_running:
                await self.cancel_channel("github")
                await self.network.replace(self.network.settings)
                self.catalog = None
                self.routes.clear()
                self.client.invalidate()
                self.github_generation += 1
            else:
                await self._stop()
            self.store.delete("auth")
            log.info("GitHub authorization removed.")
            self.credentials = self.device = None
            self.state = "running" if keep_running else "stopped" if self.any_channel_ready() else "signed-out"
            self.login_state, self.message = "idle", ""
            return self.status()

    async def set_proxy(self, settings: ProxySettings):
        async with self.lifecycle:
            if self.shutdown.is_set():
                raise RuntimeError("Service is shutting down.")
            document = read_project_config(self.store.root)
            document["proxy"] = asdict(settings)
            await self._cancel_login()
            await self._cancel_agy_login()
            await self.cancel_channel("antigravity")
            old = self.network.settings
            await self.network.replace(settings)
            await self.m365_network.replace(settings)
            try:
                atomic_write(self.store.root / "adapter-config.json", (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
            except Exception:
                await self.network.replace(old)
                await self.m365_network.replace(old)
                raise
            self.invalidate()
            log.info("Upstream proxy changed: type=%s", settings.type)
            self.message = ""
            if self.any_channel_ready():
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
            profile = self.m365_auth().profile
            if self.m365_ready() and profile is not None:
                models = await M365Client(network).models(profile)
                return {"message": f"Connection and authorization OK; {len(models)} chat models."}
            if self.agy_ready():
                models = await self.agy_client.models(self.agy_settings(), settings)
                return {"message": f"Connection and authorization OK; {len(models)} chat models."}
            await client.json("GET", "https://api.github.com", headers={"user-agent": "Github-Copilot-Proxy"})
            return {"message": "GitHub connection OK. Sign in to verify Copilot access."}
        finally:
            await network.close()

    async def execute(self, request: dict) -> Any:
        if self.shutdown.is_set():
            raise ValueError("Service is shutting down.")
        command = request.get("command")
        if command == "logging-set":
            return await self.set_logging(request.get("enabled"))
        if command == "m365-settings-get":
            return self.m365_settings()
        if command == "m365-settings-set":
            return await self.set_m365_settings(request.get("settings"))
        if command in ("m365-connect", "m365-status", "m365-capture", "m365-disconnect", "m365-verify"):
            if self._m365 is None:
                self._m365 = M365Auth(self.store)
            if command == "m365-connect":
                log.info("M365 authorization connection requested.")
                return self._m365.connect()
            if command == "m365-status":
                return self.status()["m365"]
            if command == "m365-capture":
                async with self.lifecycle:
                    result = self._m365.capture(request.get("profile"), request.get("linkId"))
                    log.info("M365 authorization received and saved.")
                    self.m365_catalog = None
                    if not self.runner:
                        self.state = "stopped"
                        if self.running_requested:
                            try:
                                await self._start()
                            except (OSError, aiohttp.ClientError, UpstreamError, ValueError, TimeoutError) as error:
                                log.warning("M365 authorization saved; service startup failed: %s", safe_error(error))
                                return {**result, "message": safe_error(error)}
                    return result
            if command == "m365-disconnect":
                async with self.lifecycle:
                    if "linkId" in request:
                        self._m365.check_link(request["linkId"])
                    await self.cancel_channel("m365")
                    await self.m365_network.replace(self.m365_network.settings)
                    result = self._m365.disconnect()
                    log.info("M365 authorization removed.")
                    self.m365_catalog = None
                    if not self.credentials and not self.agy_ready():
                        await self._stop()
                    return result
            return await self._m365.verify(self.network)
        if command == "agy-status":
            return self.status()["antigravity"]
        if command == "agy-settings-get":
            return self.agy_settings_dict()
        if command == "agy-settings-set":
            return await self.set_agy_settings(request.get("settings"))
        if command == "agy-login":
            return await self.begin_agy_login()
        if command == "agy-cancel-login":
            async with self.lifecycle:
                await self._cancel_agy_login()
                return self.status()
        if command == "agy-logout":
            return await self.agy_logout()
        if command == "agy-install":
            return await self.begin_agy_install()
        if command == "agy-cancel-install":
            async with self.lifecycle:
                await self._cancel_agy_install()
                return self.status()
        if command == "agy-install-status":
            return self.agy_installer.status()
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
                return web.json_response({"object": "list", "data": [{"id": m["id"], "name": m.get("name", m["id"]),
                                                                    "object": "model", "owned_by": m.get("owned_by", "GitHub Copilot")} for m in models]})
            port = request.transport.get_extra_info("sockname")[1]
            raw = request.path == "/v1/responses" and port == self.listener.bridge_port
            if request.method != "POST" or (request.path != "/v1/chat/completions" and not raw):
                return web.json_response({"error": {"message": "Endpoint not found"}}, status=404)
            payload = await request.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("model"), str):
                raise ProtocolError("A model id and JSON object are required.")
            channel = "m365" if payload["model"].startswith("m365/") else "antigravity" if payload["model"].startswith("agy/") else "github"
            request[ROUTE_PROVIDER] = channel
            self.active_channels[task] = channel
            fetch_models = self.m365_models if channel == "m365" else self.agy_models if channel == "antigravity" else self.github_models
            model = next((m for m in await fetch_models() if m["id"] == payload["model"]), None)
            if model is None and time.monotonic() - self.forced_at >= 10:
                self.forced_at = time.monotonic()
                model = next((m for m in await fetch_models(True) if m["id"] == payload["model"]), None)
            if model is None:
                raise ProtocolError("Model is not in the official Copilot catalog.")
            request[ROUTE_MODEL] = model["id"]
            if channel == "antigravity":
                if raw:
                    raise ProtocolError("Antigravity models support /v1/chat/completions, not /v1/responses.")
                async with self.agy_rate_lock:
                    await asyncio.sleep(max(0, self.agy_next_request - time.monotonic()))
                    self.agy_next_request = time.monotonic() + self.request_interval
                settings = self.agy_settings()
                if not self.agy_auth().ready(settings):
                    raise UpstreamError(401, "Antigravity is not enabled or signed in.")
                try:
                    async with aclosing(self.agy_client.chat(settings, self.network.settings, model, payload)) as chunks:
                        if not payload.get("stream"):
                            collected = [chunk async for chunk in chunks]
                            return web.json_response(collect_chat(collected, channel="Antigravity"))
                        first = await anext(chunks)
                        stream = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
                        await stream.prepare(request)
                        await self.write_event(stream, first)
                        async for chunk in chunks:
                            await self.write_event(stream, chunk)
                        await self.write_event(stream, "[DONE]")
                        return stream
                except UpstreamError as error:
                    if error.status == 401:
                        self.agy_auth().mark_error(safe_error(error), clear_auth=True)
                        self.agy_catalog = None
                    else:
                        self.agy_auth().mark_error(safe_error(error))
                    raise
            if channel == "m365":
                if raw:
                    raise ProtocolError("M365 models support /v1/chat/completions, not /v1/responses.")
                async with self.m365_rate_lock:
                    await asyncio.sleep(max(0, self.m365_next_request - time.monotonic()))
                    self.m365_next_request = time.monotonic() + self.request_interval
                profile = self.m365_auth().profile
                if profile is None or not self.m365_ready():
                    raise UpstreamError(401, "M365 authorization is missing or expired.")
                try:
                    async with aclosing(self.m365_client.chat(profile, model, payload)) as chunks:
                        if not payload.get("stream"):
                            collected = [chunk async for chunk in chunks]
                            return web.json_response(collect_chat(collected))
                        first = await anext(chunks)
                        stream = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
                        await stream.prepare(request)
                        await self.write_event(stream, first)
                        async for chunk in chunks:
                            await self.write_event(stream, chunk)
                        await self.write_event(stream, "[DONE]")
                        return stream
                except UpstreamError as error:
                    self.reject_m365(error, profile)
                    raise
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
            request[API_FAILED] = True
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
            self.active_channels.pop(task, None)

    @staticmethod
    async def write_event(stream, value):
        data = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        await stream.write(f"data: {data}\n\n".encode())

    async def close(self):
        try:
            await self.stop()
            await self.network.close()
            await self.m365_network.close()
        finally:
            if self.owns_log_manager:
                self.log_manager.close()


class ControlServer:
    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        self.key = secrets.token_hex(32)
        self.server = None
        self.connections = set()

    async def start(self):
        port = M365Settings.load(self.runtime.store.root).control_port
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", port, limit=65536)
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


def control_request(store: UserStore, request: dict, timeout: float = 125, *, expected_port: int = 0) -> Any:
    control = validate_control_record(store.read("control"))
    if expected_port and control["port"] != expected_port:
        raise ValueError("Control port does not match the running application. Use the same port or automatic discovery.")
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
    try:
        with runtime_logging(store, LoggingSettings.load(store.root).enabled) as logs:
            runtime = control = None
            log.info("Gateway process starting: pid=%d log_file=%s", os.getpid(), logs.path)
            try:
                runtime = Runtime(store, log_manager=logs)
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
                    log.info("Gateway process stopped.")
    finally:
        lock.close()
