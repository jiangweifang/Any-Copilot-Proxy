from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import time
from typing import AsyncIterator
from urllib.parse import urlparse
import uuid

import aiohttp
from aiohttp_socks import ProxyConnector, ProxyType

from .config import ProxySettings


class UpstreamError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class Network:
    def __init__(self, settings: ProxySettings):
        self.settings = settings
        self.session: aiohttp.ClientSession | None = None
        self.generation = 0

    def _session(self) -> aiohttp.ClientSession:
        if self.session is None:
            connector = None
            if self.settings.type == "socks5":
                connector = ProxyConnector(proxy_type=ProxyType.SOCKS5, host=self.settings.host, port=self.settings.port,
                                           username=self.settings.username or None, password=self.settings.password or None, rdns=True)
            self.session = aiohttp.ClientSession(connector=connector, trust_env=False,
                                                timeout=aiohttp.ClientTimeout(total=300, sock_connect=15))
        return self.session

    @asynccontextmanager
    async def request(self, method: str, url: str, **kwargs) -> AsyncIterator[aiohttp.ClientResponse]:
        session = self._session()
        if self.settings.type == "http":
            host = f"[{self.settings.host}]" if ":" in self.settings.host else self.settings.host
            kwargs["proxy"] = f"http://{host}:{self.settings.port}"
            if self.settings.username or self.settings.password:
                authorization = aiohttp.encode_basic_auth(self.settings.username, self.settings.password)
                if urlparse(url).scheme == "https":
                    kwargs["proxy_headers"] = {"Proxy-Authorization": authorization}
                else:
                    kwargs["headers"] = {**kwargs.get("headers", {}), "Proxy-Authorization": authorization}
        async with session.request(method, url, allow_redirects=False, **kwargs) as response:
            yield response

    async def replace(self, settings: ProxySettings) -> None:
        old = self.session
        self.settings = settings
        self.session = None
        self.generation += 1
        if old:
            await old.close()

    async def close(self) -> None:
        if self.session:
            await self.session.close()
            self.session = None


IDENTITY = {
    "accept": "application/json",
    "content-type": "application/json",
    "editor-version": "vscode/1.104.3",
    "editor-plugin-version": "copilot-chat/0.26.7",
    "user-agent": "GitHubCopilotChat/0.26.7",
}


@dataclass(frozen=True)
class DeviceCode:
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int = 5


class CopilotClient:
    def __init__(self, network: Network):
        self.network = network
        self.session_token = ""
        self.endpoint = ""
        self.expires = 0.0
        self.token_lock = asyncio.Lock()

    def invalidate(self) -> None:
        self.session_token = ""
        self.endpoint = ""
        self.expires = 0

    async def json(self, method: str, url: str, **kwargs) -> dict:
        async with self.network.request(method, url, timeout=aiohttp.ClientTimeout(total=30, sock_connect=15), **kwargs) as response:
            if response.status >= 300:
                raise UpstreamError(response.status, f"Upstream returned HTTP {response.status}.")
            data = await response.json(content_type=None)
            if not isinstance(data, dict):
                raise ValueError("The upstream returned an invalid JSON object.")
            return data

    async def begin_login(self) -> DeviceCode:
        result = await self.json("POST", "https://github.com/login/device/code", headers=IDENTITY,
                                 json={"client_id": "Iv1.b507a08c87ecfe98", "scope": "read:user"})
        code = DeviceCode(result.get("device_code", ""), result.get("user_code", ""), result.get("verification_uri", ""),
                          result.get("expires_in", 0), result.get("interval", 5))
        if not code.device_code or not code.user_code or code.verification_uri != "https://github.com/login/device":
            raise ValueError("Invalid device authorization response.")
        if type(code.expires_in) is not int or type(code.interval) is not int or code.expires_in <= 0 or code.interval <= 0:
            raise ValueError("Invalid device authorization timeout.")
        return code

    async def username(self, github_token: str) -> str:
        profile = await self.json("GET", "https://api.github.com/user",
                                  headers={**IDENTITY, "authorization": f"token {github_token}",
                                           "x-github-api-version": "2025-04-01"})
        username = profile.get("login")
        if not isinstance(username, str) or not username.strip() or len(username) > 256:
            raise ValueError("GitHub profile did not contain a valid username.")
        return username.strip()

    async def complete_login(self, code: DeviceCode) -> str:
        interval = code.interval
        async with asyncio.timeout(code.expires_in):
            while True:
                await asyncio.sleep(interval)
                result = await self.json("POST", "https://github.com/login/oauth/access_token", headers=IDENTITY,
                                         json={"client_id": "Iv1.b507a08c87ecfe98", "device_code": code.device_code,
                                               "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
                if isinstance(result.get("access_token"), str) and result["access_token"]:
                    await self.authorize(result["access_token"], force=True)
                    return result["access_token"]
                error = result.get("error")
                if error == "authorization_pending":
                    continue
                if error == "slow_down":
                    interval += 5
                    continue
                if error == "access_denied":
                    raise ValueError("GitHub authorization was denied.")
                if error == "expired_token":
                    raise ValueError("GitHub authorization expired.")
                raise ValueError("GitHub authorization failed.")

    async def authorize(self, github_token: str, force: bool = False) -> tuple[str, str]:
        async with self.token_lock:
            if not force and self.session_token and self.expires > time.monotonic() + 60:
                return self.session_token, self.endpoint
            generation = self.network.generation
            result = await self.json("GET", "https://api.github.com/copilot_internal/v2/token",
                                     headers={**IDENTITY, "authorization": f"token {github_token}", "x-github-api-version": "2025-04-01"})
            endpoint = result.get("endpoints", {}).get("api", "")
            uri = urlparse(endpoint)
            if (uri.scheme != "https" or uri.hostname not in
                ("api.githubcopilot.com", "api.business.githubcopilot.com", "api.enterprise.githubcopilot.com")
                or uri.username or uri.password or uri.port not in (None, 443) or not result.get("token")):
                raise ValueError("Unexpected Copilot authorization endpoint.")
            if generation != self.network.generation:
                raise ValueError("Network changed during authorization; retry the request.")
            self.session_token = result["token"]
            self.endpoint = f"https://{uri.hostname}"
            self.expires = time.monotonic() + min(max(result.get("refresh_in", 300), 1), 1800)
            return self.session_token, self.endpoint

    @asynccontextmanager
    async def send(self, token: str, path: str, payload: dict | None = None):
        access, endpoint = await self.authorize(token)
        headers = {**IDENTITY, "authorization": f"Bearer {access}", "copilot-integration-id": "vscode-chat",
                   "openai-intent": "conversation-panel", "x-github-api-version": "2026-06-01", "x-request-id": str(uuid.uuid4()),
                   "X-Initiator": "agent" if any(m.get("role") in ("assistant", "tool") for m in (payload or {}).get("messages", [])) else "user"}
        async with self.network.request("POST" if payload is not None else "GET", endpoint + path, headers=headers, json=payload) as response:
            yield response

    async def models(self, token: str) -> list[dict]:
        async with self.send(token, "/models") as response:
            if response.status >= 300:
                raise UpstreamError(response.status, "Cannot fetch Copilot models.")
            result = await response.json(content_type=None)
        if not isinstance(result.get("data"), list):
            raise ValueError("Invalid Copilot model catalog.")
        models = [model for model in result["data"] if model.get("model_picker_enabled")
                  and model.get("capabilities", {}).get("type") != "embeddings" and isinstance(model.get("id"), str)
                  and (not model.get("supported_endpoints") or
                       set(model["supported_endpoints"]) & {"/responses", "/chat/completions"})]
        if not models:
            raise ValueError("No supported chat models are available.")
        return models
