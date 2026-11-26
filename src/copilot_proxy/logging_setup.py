from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
import logging
from logging.handlers import RotatingFileHandler
import re
import time
from urllib.parse import urlsplit, urlunsplit
import uuid

from aiohttp import web

from .config import UserStore

request_id = ContextVar("copilot_request_id", default="-")
log = logging.getLogger("copilot_proxy.requests")
API_PATHS = frozenset(("/v1/models", "/v1/chat/completions", "/v1/responses"))
REQUEST_ID = web.RequestKey("request_id", str)
ROUTE_PROVIDER = web.RequestKey("route_provider", str)
ROUTE_MODEL = web.RequestKey("route_model", str)
API_FAILED = web.RequestKey("api_failed", bool)


def redact(message: str) -> str:
    def clean_url(match):
        try:
            uri = urlsplit(match.group(0))
            host = uri.hostname or ""
            if ":" in host:
                host = f"[{host}]"
            authority = host + (f":{uri.port}" if uri.port else "")
            return urlunsplit((uri.scheme, authority, uri.path, "[redacted]" if uri.query else "", ""))
        except ValueError:
            return "[redacted-url]"

    message = re.sub(r"\b(?:https?|wss?|socks5)://[^\s<>\"']+", clean_url, message)
    message = re.sub(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/\-=]+", r"\1 [redacted]", message)
    message = re.sub(r'(?i)(["\']?(?:access_token|refresh_token|api[_-]?key|password|client_secret|authorization|token)["\']?\s*[:=]\s*)'
                     r'(?:"[^"]*"|\'[^\']*\'|[^\s,;}]+)', r'\1[redacted]', message)
    message = re.sub(r"\beyJ[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]*){2,4}\b", "[redacted-token]", message)
    return re.sub(r"\b(?:gh[a-z]_|sk-)[A-Za-z0-9_-]{16,}", "[redacted-key]", message)


class PrivateFormatter(logging.Formatter):
    def format(self, record):
        record.request_id = request_id.get()
        return redact(super().format(record))


class RuntimeLog:
    def __init__(self, store: UserStore):
        self.path = store.directory / "logs" / "gateway.log"
        self.logger = logging.getLogger("copilot_proxy")
        self.handler: RotatingFileHandler | None = None
        self.previous_level = self.logger.level

    @property
    def enabled(self) -> bool:
        return self.handler is not None

    def set_enabled(self, enabled: bool):
        if enabled == self.enabled:
            return
        if enabled:
            self.path.parent.mkdir(mode=0o700, exist_ok=True)
            handler = RotatingFileHandler(self.path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
            handler.setFormatter(PrivateFormatter("%(asctime)s %(levelname)s %(name)s request=%(request_id)s %(message)s",
                                                 datefmt="%Y-%m-%d %H:%M:%S"))
            self.previous_level = self.logger.level
            self.logger.setLevel(logging.INFO)
            self.logger.addHandler(handler)
            self.handler = handler
            self.logger.info("Runtime file logging enabled.")
        else:
            self.logger.info("Runtime file logging disabled; existing files are retained.")
            handler = self.handler
            if handler is not None:
                self.logger.removeHandler(handler)
                handler.close()
            self.handler = None
            self.logger.setLevel(self.previous_level)

    def close(self):
        self.set_enabled(False)


@contextmanager
def runtime_logging(store: UserStore, enabled: bool = True):
    manager = RuntimeLog(store)
    manager.set_enabled(enabled)
    try:
        yield manager
    finally:
        manager.close()


@web.middleware
async def request_logging(request: web.Request, handler):
    identity = uuid.uuid4().hex
    request[REQUEST_ID] = identity
    token = request_id.set(identity)
    started = time.monotonic()
    endpoint = request.path if request.path in API_PATHS else "<unmatched>"
    method = request.method if request.method in ("GET", "POST", "OPTIONS", "HEAD") else "<other>"
    response = None
    outcome = "error"
    log.info("API started method=%s endpoint=%s", method, endpoint)
    try:
        response = await handler(request)
        outcome = "error" if request.get(API_FAILED) or response.status >= 400 else "ok"
        return response
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    finally:
        model = request.get(ROUTE_MODEL, "-")
        if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9_.@/-]{1,160}", model):
            model = "<unrecognized>"
        log.info("API finished provider=%s model=%s status=%s outcome=%s duration_ms=%d",
                 request.get(ROUTE_PROVIDER, "-"), model, response.status if response is not None else "interrupted",
                 outcome, round((time.monotonic() - started) * 1000))
        request_id.reset(token)


async def response_request_id(request: web.Request, response: web.StreamResponse):
    if REQUEST_ID in request:
        response.headers["X-Request-ID"] = request[REQUEST_ID]
