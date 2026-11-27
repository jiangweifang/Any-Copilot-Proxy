from __future__ import annotations

import logging
from pathlib import Path
import shutil
import sys
import tempfile
import time
from typing import Any

from ..config import AntigravitySettings, ProxySettings, UserStore
from ..network import UpstreamError
from .acp import AcpConnection, build_subprocess_env
from .installer import default_binary_path

log = logging.getLogger(__name__)

CLIENT_INFO = {"name": "any-copilot-proxy", "version": "0.1.0"}
DEFAULT_AUTH_METHOD = "oauth-personal"


def resolve_binary(configured_path: str = "") -> str | None:
    candidate = configured_path.strip()
    if candidate:
        path = Path(candidate).expanduser()
        if path.is_file():
            return str(path.resolve())
        return None
    names = ("agy.exe", "agy.cmd", "agy") if sys.platform == "win32" else ("agy",)
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    managed = default_binary_path()
    if managed.is_file():
        return str(managed.resolve())
    return None


def acp_command(binary: str, *, store: UserStore | None = None, profile_mode: str = "shared") -> list[str]:
    cmd = [binary, "acp"]
    if profile_mode == "isolated" and store is not None:
        base = store.directory / "antigravity"
        gemini_dir = base / "gemini"
        app_data_dir = base / "app-data"
        gemini_dir.mkdir(parents=True, exist_ok=True)
        app_data_dir.mkdir(parents=True, exist_ok=True)
        cmd.extend(["--gemini-dir", str(gemini_dir), "--app-data-dir", str(app_data_dir)])
    return cmd


def extract_models(session_result: dict[str, Any] | None, init_result: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    discovered: list[dict[str, Any]] = []
    seen: set[str] = set()
    config_option_id: str | None = None
    reasoning_option_id: str | None = None
    reasoning_values: list[str] = []

    sources = [item for item in (session_result, init_result) if isinstance(item, dict)]

    for source in sources:
        for opt in source.get("configOptions") or []:
            if not isinstance(opt, dict):
                continue
            category = str(opt.get("category") or opt.get("id") or "").lower()
            if category in ("thought_level", "reasoning_effort", "reasoning"):
                reasoning_option_id = str(opt.get("id") or "")
                reasoning_values = [
                    str(choice["value"])
                    for choice in (opt.get("options") or [])
                    if isinstance(choice, dict) and isinstance(choice.get("value"), str)
                ]

    def register(raw_id: str, name: str | None = None, *, config_id: str | None = None) -> None:
        cleaned = raw_id.strip()
        if not cleaned or cleaned in seen:
            return
        seen.add(cleaned)
        entry: dict[str, Any] = {
            "id": cleaned if cleaned.startswith("agy/") else f"agy/{cleaned}",
            "raw_id": cleaned[4:] if cleaned.startswith("agy/") else cleaned,
            "name": (name or cleaned).strip(),
            "owned_by": "Google Antigravity",
        }
        if config_id:
            entry["config_option_id"] = config_id
        if reasoning_option_id:
            entry["reasoning_option_id"] = reasoning_option_id
            entry["reasoning_values"] = list(reasoning_values)
        discovered.append(entry)

    for source in sources:
        models_block = source.get("models")
        if isinstance(models_block, dict):
            for item in models_block.get("availableModels") or []:
                if isinstance(item, dict) and isinstance(item.get("modelId"), str):
                    register(item["modelId"], item.get("name"))
        for item in source.get("availableModels") or []:
            if isinstance(item, dict) and isinstance(item.get("modelId"), str):
                register(item["modelId"], item.get("name"))
            elif isinstance(item, dict) and isinstance(item.get("id"), str):
                register(item["id"], item.get("name"))
        for opt in source.get("configOptions") or []:
            if not isinstance(opt, dict):
                continue
            category = str(opt.get("category") or opt.get("id") or "").lower()
            if category == "model":
                config_option_id = str(opt.get("id") or "model")
                for choice in opt.get("options") or []:
                    if isinstance(choice, dict) and isinstance(choice.get("value"), str):
                        register(choice["value"], choice.get("name"), config_id=config_option_id)

    return discovered


def select_auth_method(init_result: dict[str, Any] | None) -> str:
    if not isinstance(init_result, dict):
        return DEFAULT_AUTH_METHOD
    methods = init_result.get("authMethods") or []
    ids = [m.get("id") for m in methods if isinstance(m, dict) and isinstance(m.get("id"), str)]
    if DEFAULT_AUTH_METHOD in ids:
        return DEFAULT_AUTH_METHOD
    for method_id in ids:
        if "oauth" in method_id.lower():
            return method_id
    return ids[0] if ids else DEFAULT_AUTH_METHOD


class AntigravityAuth:
    STORE_KEY = "antigravity_auth"

    def __init__(self, store: UserStore, *, command_factory=None):
        self.store = store
        self.command_factory = command_factory or acp_command
        self.login_state = "idle"
        self.last_error = ""
        self.generation = 0
        self._record: dict[str, Any] | None = self._load()

    def _load(self) -> dict[str, Any] | None:
        data = self.store.read(self.STORE_KEY)
        if isinstance(data, dict) and data.get("authenticated") is True:
            return data
        return None

    @property
    def authenticated(self) -> bool:
        return self._record is not None

    @property
    def account(self) -> str | None:
        if not self._record:
            return None
        value = self._record.get("account")
        return value if isinstance(value, str) and value.strip() else None

    def ready(self, settings: AntigravitySettings) -> bool:
        return settings.enabled and self.authenticated and resolve_binary(settings.binary_path) is not None

    def status(self, settings: AntigravitySettings) -> dict[str, Any]:
        resolved = resolve_binary(settings.binary_path)
        return {
            "enabled": settings.enabled,
            "binaryPath": settings.binary_path,
            "resolvedBinary": resolved,
            "binaryFound": resolved is not None,
            "profileMode": settings.profile_mode,
            "authenticated": self.authenticated,
            "ready": settings.enabled and self.authenticated and resolved is not None,
            "loginState": self.login_state,
            "authMethod": (self._record or {}).get("method", DEFAULT_AUTH_METHOD),
            "account": self.account,
            "lastError": self.last_error,
        }

    def save_authenticated(self, *, method: str = DEFAULT_AUTH_METHOD, account: str | None = None) -> dict[str, Any]:
        record: dict[str, Any] = {
            "authenticated": True,
            "method": method,
            "updatedAt": int(time.time()),
        }
        if account:
            record["account"] = account
        self.store.write(self.STORE_KEY, record)
        self._record = record
        self.generation += 1
        self.last_error = ""
        return record

    def clear(self) -> None:
        self.store.delete(self.STORE_KEY)
        self._record = None
        self.generation += 1
        self.login_state = "idle"
        self.last_error = ""

    def mark_error(self, message: str, *, clear_auth: bool = False) -> None:
        self.last_error = message
        if clear_auth:
            self.store.delete(self.STORE_KEY)
            self._record = None
            self.generation += 1

    async def authenticate(
        self,
        settings: AntigravitySettings,
        proxy: ProxySettings,
        *,
        timeout: float = 300.0,
    ) -> dict[str, Any]:
        binary = resolve_binary(settings.binary_path)
        if binary is None:
            raise UpstreamError(
                503,
                "Antigravity CLI ('agy') was not found. Install it or configure antigravity.binaryPath.",
            )
        env = build_subprocess_env(proxy, store=self.store, profile_mode=settings.profile_mode)
        with tempfile.TemporaryDirectory(prefix="agy-auth-") as scratch:
            cmd = (
                self.command_factory(binary, store=self.store, profile_mode=settings.profile_mode)
                if self.command_factory is acp_command
                else self.command_factory(binary)
            )
            async with AcpConnection(cmd, env=env, cwd=scratch) as conn:
                init_result = await conn.request(
                    "initialize",
                    {"protocolVersion": 1, "clientCapabilities": {}, "clientInfo": CLIENT_INFO},
                    timeout=30.0,
                )
                method_id = select_auth_method(init_result)
                auth_result = await conn.request(
                    "authenticate",
                    {"methodId": method_id},
                    timeout=timeout,
                )
                session_result = await conn.request(
                    "session/new",
                    {"cwd": scratch, "mcpServers": []},
                    timeout=30.0,
                )
                account = None
                if isinstance(auth_result, dict):
                    for key in ("email", "account", "username", "user"):
                        val = auth_result.get(key)
                        if isinstance(val, str) and val.strip():
                            account = val.strip()
                            break
                models = extract_models(session_result, init_result)
                self.save_authenticated(method=method_id, account=account)
                log.info("Antigravity authorization completed: method=%s models=%d", method_id, len(models))
                return {"method": method_id, "account": account, "models": models}
