from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
from typing import Any
from urllib.parse import quote

from ..config import ProxySettings, UserStore
from ..network import UpstreamError

log = logging.getLogger(__name__)

STREAM_LIMIT = 4 * 1024 * 1024
MAX_STDERR_CHARS = 4096
PROXY_ENV_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


def proxy_url(settings: ProxySettings) -> str | None:
    if settings.type == "none":
        return None
    scheme = "http" if settings.type == "http" else "socks5h"
    auth = ""
    if settings.username or settings.password:
        auth = f"{quote(settings.username, safe='')}:{quote(settings.password, safe='')}@"
    host = f"[{settings.host}]" if ":" in settings.host and not settings.host.startswith("[") else settings.host
    return f"{scheme}://{auth}{host}:{settings.port}"


def build_subprocess_env(
    proxy: ProxySettings,
    *,
    store: UserStore | None = None,
    profile_mode: str = "shared",
) -> dict[str, str]:
    env = os.environ.copy()
    url = proxy_url(proxy)
    if url is None:
        for key in PROXY_ENV_KEYS:
            env.pop(key, None)
    else:
        for key in PROXY_ENV_KEYS:
            env[key] = url
    if profile_mode == "isolated" and store is not None:
        profile_dir = store.directory / "antigravity"
        profile_dir.mkdir(parents=True, exist_ok=True)
        env["ANTIGRAVITY_HOME"] = str(profile_dir)
        env["ANTIGRAVITY_CONFIG_DIR"] = str(profile_dir)
        env["XDG_CONFIG_HOME"] = str(profile_dir)
    return env


def classify_rpc_error(error: dict[str, Any]) -> UpstreamError:
    code = error.get("code")
    message = str(error.get("message") or "Antigravity ACP request failed.")
    lower = message.lower()
    if code in (401, -32000) or any(word in lower for word in ("unauthenticated", "unauthorized", "not authenticated", "sign in", "login required", "auth required")):
        return UpstreamError(401, f"Antigravity authentication required: {message}")
    if code == 403 or any(word in lower for word in ("ineligible", "forbidden", "permission denied", "subscription")):
        return UpstreamError(403, f"Antigravity access denied: {message}")
    if code == 429 or any(word in lower for word in ("quota", "rate limit", "resource_exhausted", "too many requests")):
        return UpstreamError(429, f"Antigravity rate or quota limit reached: {message}")
    return UpstreamError(502, f"Antigravity ACP error: {message}")


class AcpConnection:
    """Async JSON-RPC 2.0 over stdio client for `agy acp`."""

    def __init__(
        self,
        command: list[str],
        *,
        env: dict[str, str] | None = None,
        cwd: str | Path | None = None,
    ):
        self.command = list(command)
        self.env = env
        self.cwd = str(cwd) if cwd is not None else None
        self.process: asyncio.subprocess.Process | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._updates: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._stdout_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()
        self._stderr_tail = ""
        self._closed = False

    async def __aenter__(self) -> AcpConnection:
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def start(self) -> None:
        kwargs: dict[str, Any] = {
            "stdin": asyncio.subprocess.PIPE,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
            "limit": STREAM_LIMIT,
            "env": self.env,
            "cwd": self.cwd,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            self.process = await asyncio.create_subprocess_exec(*self.command, **kwargs)
        except FileNotFoundError as error:
            raise UpstreamError(503, f"Antigravity CLI executable not found: {self.command[0]}") from error
        except OSError as error:
            raise UpstreamError(502, f"Failed to start Antigravity CLI: {error}") from error
        self._stdout_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())

    async def _send(self, payload: dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None or self._closed:
            raise UpstreamError(502, "Antigravity ACP connection is closed.")
        line = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        async with self._write_lock:
            try:
                self.process.stdin.write(line)
                await self.process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError, OSError) as error:
                raise UpstreamError(502, "Antigravity ACP process closed stdin unexpectedly.") from error

    async def request(self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 60.0) -> Any:
        self._next_id += 1
        req_id = self._next_id
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._pending[req_id] = future
        try:
            await self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}})
            async with asyncio.timeout(timeout):
                return await future
        except TimeoutError as error:
            self._pending.pop(req_id, None)
            raise UpstreamError(504, f"Antigravity ACP request '{method}' timed out.") from error
        except BaseException:
            self._pending.pop(req_id, None)
            raise

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    async def next_update(self) -> dict[str, Any] | None:
        return await self._updates.get()

    def drain_updates(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        while True:
            try:
                item = self._updates.get_nowait()
            except asyncio.QueueEmpty:
                break
            if item is not None:
                items.append(item)
        return items

    async def _handle_agent_request(self, message: dict[str, Any]) -> None:
        req_id = message["id"]
        method = message.get("method", "")
        if method == "session/request_permission":
            log.info("Denied Antigravity built-in tool permission request in gateway mode.")
            await self._send({"jsonrpc": "2.0", "id": req_id, "result": {"outcome": {"outcome": "cancelled"}}})
            return
        log.info("Rejected Antigravity client RPC '%s' in gateway mode.", method)
        await self._send({
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": "Client tool execution is disabled in gateway mode."},
        })

    async def _read_stdout(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        try:
            while True:
                raw = await self.process.stdout.readline()
                if not raw:
                    break
                text = raw.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    message = json.loads(text)
                except json.JSONDecodeError:
                    log.debug("Ignoring non-JSON stdout line from agy acp.")
                    continue
                if not isinstance(message, dict):
                    continue
                if "id" in message and "method" in message:
                    with suppress(UpstreamError):
                        await self._handle_agent_request(message)
                    continue
                if "id" in message:
                    future = self._pending.pop(message["id"], None)
                    if future is not None and not future.done():
                        if "error" in message and message["error"] is not None:
                            err = message["error"] if isinstance(message["error"], dict) else {"message": str(message["error"])}
                            future.set_exception(classify_rpc_error(err))
                        else:
                            future.set_result(message.get("result"))
                    continue
                if message.get("method") == "session/update" and isinstance(message.get("params"), dict):
                    await self._updates.put(message["params"])
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._fail_pending(UpstreamError(502, f"Antigravity ACP stream error: {error}"))
        finally:
            if not self._closed:
                detail = self._stderr_tail.strip()
                suffix = f" ({detail[-240:]})" if detail else ""
                self._fail_pending(UpstreamError(502, f"Antigravity ACP process exited unexpectedly.{suffix}"))
            await self._updates.put(None)

    async def _read_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        try:
            while True:
                chunk = await self.process.stderr.read(4096)
                if not chunk:
                    break
                text = chunk.decode("utf-8", errors="replace")
                self._stderr_tail = (self._stderr_tail + text)[-MAX_STDERR_CHARS:]
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    def _fail_pending(self, error: BaseException) -> None:
        pending = list(self._pending.values())
        self._pending.clear()
        for future in pending:
            if not future.done():
                future.set_exception(error)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        proc = self.process
        if proc is not None:
            if proc.stdin is not None:
                with suppress(OSError):
                    proc.stdin.close()
            if proc.returncode is None:
                try:
                    async with asyncio.timeout(1.5):
                        await proc.wait()
                except TimeoutError:
                    with suppress(ProcessLookupError):
                        proc.terminate()
                    try:
                        async with asyncio.timeout(1.5):
                            await proc.wait()
                    except TimeoutError:
                        with suppress(ProcessLookupError):
                            proc.kill()
                        with suppress(OSError):
                            await proc.wait()
        for task in (self._stdout_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        self._fail_pending(UpstreamError(502, "Antigravity ACP connection closed."))
