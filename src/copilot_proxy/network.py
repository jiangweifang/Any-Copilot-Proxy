from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator
from urllib.parse import urlparse

import aiohttp
from aiohttp_socks import ProxyConnector, ProxyType
from yarl import URL

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
        self.websocket_hosts: tuple[str, ...] = ()

    def _session(self) -> aiohttp.ClientSession:
        if self.session is None:
            connector = None
            if self.settings.type == "socks5":
                connector = ProxyConnector(proxy_type=ProxyType.SOCKS5, host=self.settings.host, port=self.settings.port,
                                           username=self.settings.username or None, password=self.settings.password or None, rdns=True)
            trace = aiohttp.TraceConfig()
            trace.on_request_redirect.append(self._check_websocket_redirect)
            self.session = aiohttp.ClientSession(connector=connector, trust_env=False, trace_configs=[trace],
                                                timeout=aiohttp.ClientTimeout(total=300, sock_connect=15))
        return self.session

    async def _check_websocket_redirect(self, session, context, params):
        if params.headers.get("Upgrade", "").lower() == "websocket":
            target = params.url.join(URL(params.response.headers.get("Location", "")))
            allowed = self.websocket_hosts or (params.url.host,)
            if (target.scheme not in ("wss", "https") or target.host not in allowed or
                    target.port != 443 or target.user is not None or target.password is not None):
                raise UpstreamError(502, "WebSocket redirected to an unexpected endpoint.")

    def request_options(self, url: str, kwargs: dict) -> dict:
        if self.settings.type == "http":
            host = f"[{self.settings.host}]" if ":" in self.settings.host else self.settings.host
            kwargs["proxy"] = f"http://{host}:{self.settings.port}"
            if self.settings.username or self.settings.password:
                authorization = aiohttp.encode_basic_auth(self.settings.username, self.settings.password)
                if urlparse(url).scheme in ("https", "wss"):
                    kwargs["proxy_headers"] = {"Proxy-Authorization": authorization}
                else:
                    kwargs["headers"] = {**kwargs.get("headers", {}), "Proxy-Authorization": authorization}
        return kwargs

    @asynccontextmanager
    async def request(self, method: str, url: str, **kwargs) -> AsyncIterator[aiohttp.ClientResponse]:
        kwargs = self.request_options(url, kwargs)
        async with self._session().request(method, url, allow_redirects=False, **kwargs) as response:
            yield response

    @asynccontextmanager
    async def websocket(self, url: str, **kwargs):
        kwargs = self.request_options(url, kwargs)
        async with self._session().ws_connect(url, timeout=aiohttp.ClientWSTimeout(ws_receive=90, ws_close=3),
                                             heartbeat=25, max_msg_size=16 * 1024 * 1024, **kwargs) as socket:
            yield socket

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
