from __future__ import annotations

import asyncio
import base64
import binascii
from contextlib import suppress
import logging
import re
import tempfile
import time
from typing import Any, AsyncIterator
import uuid

from ..chat_bridge import ChatInput, ToolStream
from ..config import AntigravitySettings, ProxySettings, UserStore
from ..network import UpstreamError
from ..protocol import ProtocolError
from .acp import AcpConnection, build_subprocess_env
from .auth import CLIENT_INFO, acp_command, extract_models, resolve_binary

log = logging.getLogger(__name__)

AGY_HINT_WARNING = (
    "Antigravity exposes no native sampler or token-limit control; "
    "generation preferences are prompt hints, not strict limits."
)
DATA_URL_RE = re.compile(r"^data:(image/(?:png|jpeg|gif|webp|bmp));base64,([A-Za-z0-9+/=\s]+)$", re.IGNORECASE)
MAX_IMAGE_BYTES = 10 * 1024 * 1024


def parse_image_block(url: str) -> dict[str, Any]:
    match = DATA_URL_RE.match(url.strip())
    if not match:
        raise ProtocolError("Antigravity accepts base64 data URLs (PNG, JPEG, GIF, WebP, BMP) for image_url.")
    mime = match.group(1).lower()
    raw_b64 = re.sub(r"\s+", "", match.group(2))
    try:
        decoded = base64.b64decode(raw_b64, validate=True)
    except (binascii.Error, ValueError):
        raise ProtocolError("Invalid base64 image data.") from None
    if not decoded or len(decoded) > MAX_IMAGE_BYTES:
        raise ProtocolError("Images must be non-empty and at most 10 MiB.")
    return {"type": "image", "data": raw_b64, "mimeType": mime}


def extract_chunk_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if content.get("type") == "text" and isinstance(content.get("text"), str):
            return content["text"]
        if isinstance(content.get("text"), str):
            return content["text"]
    return ""


class AntigravityClient:
    def __init__(self, store: UserStore, *, command_factory=None, prompt_timeout: float = 300.0):
        self.store = store
        self.command_factory = command_factory or acp_command
        self.prompt_timeout = prompt_timeout

    def _binary(self, settings: AntigravitySettings) -> str:
        binary = resolve_binary(settings.binary_path)
        if binary is None:
            raise UpstreamError(
                503,
                "Antigravity CLI ('agy') was not found. Install it or configure antigravity.binaryPath.",
            )
        return binary

    def _command(self, binary: str, settings: AntigravitySettings) -> list[str]:
        if self.command_factory is acp_command:
            return acp_command(binary, store=self.store, profile_mode=settings.profile_mode)
        return self.command_factory(binary)

    async def models(self, settings: AntigravitySettings, proxy: ProxySettings) -> list[dict[str, Any]]:
        binary = self._binary(settings)
        env = build_subprocess_env(proxy, store=self.store, profile_mode=settings.profile_mode)
        with tempfile.TemporaryDirectory(prefix="agy-models-") as scratch:
            async with AcpConnection(self._command(binary, settings), env=env, cwd=scratch) as conn:
                init_result = await conn.request(
                    "initialize",
                    {"protocolVersion": 1, "clientCapabilities": {}, "clientInfo": CLIENT_INFO},
                    timeout=30.0,
                )
                session_result = await conn.request(
                    "session/new",
                    {"cwd": scratch, "mcpServers": []},
                    timeout=30.0,
                )
                models = extract_models(session_result, init_result)
                if not models:
                    raise UpstreamError(503, "No Antigravity models are available for this account.")
                return models

    async def chat(
        self,
        settings: AntigravitySettings,
        proxy: ProxySettings,
        model: dict[str, Any],
        payload: dict[str, Any],
    ) -> AsyncIterator[dict[str, Any]]:
        parsed = ChatInput.parse(
            payload,
            channel="Antigravity",
            max_images=4,
            hint_warning=AGY_HINT_WARNING,
        )
        binary = self._binary(settings)
        env = build_subprocess_env(proxy, store=self.store, profile_mode=settings.profile_mode)
        raw_model_id = model.get("raw_id") or (model["id"][4:] if model["id"].startswith("agy/") else model["id"])
        response_id = "chatcmpl-" + uuid.uuid4().hex
        created = int(time.time())
        model_id = model["id"]
        tool_stream = ToolStream(parsed, channel="Antigravity")
        emitted_role = False

        def make_chunk(delta: dict[str, Any], finish_reason: str | None = None) -> dict[str, Any]:
            nonlocal emitted_role
            out_delta = dict(delta)
            first_chunk = not emitted_role
            if first_chunk and finish_reason is None:
                out_delta = {"role": "assistant", **out_delta}
                emitted_role = True
            chunk: dict[str, Any] = {
                "id": response_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model_id,
                "choices": [{"index": 0, "delta": out_delta, "finish_reason": finish_reason}],
            }
            if first_chunk and parsed.warnings:
                chunk["warnings"] = list(parsed.warnings)
            return chunk

        with tempfile.TemporaryDirectory(prefix="agy-chat-") as scratch:
            async with AcpConnection(self._command(binary, settings), env=env, cwd=scratch) as conn:
                init_result = await conn.request(
                    "initialize",
                    {"protocolVersion": 1, "clientCapabilities": {}, "clientInfo": CLIENT_INFO},
                    timeout=30.0,
                )
                prompt_caps = (init_result or {}).get("agentCapabilities", {}).get("promptCapabilities", {}) if isinstance(init_result, dict) else {}
                prompt_blocks: list[dict[str, Any]] = [{"type": "text", "text": parsed.prompt}]
                if parsed.images:
                    if not prompt_caps.get("image"):
                        raise ProtocolError("Antigravity ACP server did not declare image input support.")
                    for image_url in parsed.images:
                        prompt_blocks.append(parse_image_block(image_url))

                session_result = await conn.request(
                    "session/new",
                    {"cwd": scratch, "mcpServers": []},
                    timeout=30.0,
                )
                if not isinstance(session_result, dict) or not isinstance(session_result.get("sessionId"), str):
                    raise UpstreamError(502, "Antigravity ACP did not return a valid sessionId.")
                session_id = session_result["sessionId"]

                current_model = None
                if isinstance(session_result.get("models"), dict):
                    current_model = session_result["models"].get("currentModelId")
                if current_model != raw_model_id:
                    config_opt_id = model.get("config_option_id")
                    if config_opt_id:
                        await conn.request(
                            "session/set_config_option",
                            {"sessionId": session_id, "configId": config_opt_id, "value": raw_model_id},
                            timeout=20.0,
                        )
                    else:
                        await conn.request(
                            "session/set_model",
                            {"sessionId": session_id, "modelId": raw_model_id},
                            timeout=20.0,
                        )

                if parsed.reasoning_effort and model.get("reasoning_option_id"):
                    allowed = model.get("reasoning_values") or []
                    if not allowed or parsed.reasoning_effort in allowed:
                        with suppress(UpstreamError):
                            await conn.request(
                                "session/set_config_option",
                                {
                                    "sessionId": session_id,
                                    "configId": model["reasoning_option_id"],
                                    "value": parsed.reasoning_effort,
                                },
                                timeout=15.0,
                            )

                prompt_task = asyncio.create_task(
                    conn.request(
                        "session/prompt",
                        {"sessionId": session_id, "prompt": prompt_blocks},
                        timeout=self.prompt_timeout,
                    )
                )

                def convert_update(params: dict[str, Any]) -> list[dict[str, Any]]:
                    if params.get("sessionId") not in (None, session_id):
                        return []
                    update = params.get("update")
                    if not isinstance(update, dict):
                        return []
                    kind = update.get("sessionUpdate")
                    if kind == "agent_thought_chunk":
                        thought = extract_chunk_text(update.get("content"))
                        return [make_chunk({"reasoning_content": thought})] if thought else []
                    if kind == "agent_message_chunk":
                        text = extract_chunk_text(update.get("content"))
                        if not text:
                            return []
                        return [make_chunk(delta) for delta in tool_stream.push(text)]
                    return []

                try:
                    while True:
                        update_task = asyncio.create_task(conn.next_update())
                        done, _ = await asyncio.wait(
                            {prompt_task, update_task},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if update_task in done:
                            params = update_task.result()
                            if params is None:
                                break
                            for chunk in convert_update(params):
                                yield chunk
                        else:
                            update_task.cancel()
                            with suppress(asyncio.CancelledError):
                                await update_task
                        if prompt_task in done:
                            break

                    await prompt_task
                    for params in conn.drain_updates():
                        for chunk in convert_update(params):
                            yield chunk
                    for delta in tool_stream.finish():
                        yield make_chunk(delta)
                    if not emitted_role:
                        yield make_chunk({"content": ""})
                    finish_reason = "tool_calls" if tool_stream.calls else "stop"
                    yield make_chunk({}, finish_reason=finish_reason)
                except asyncio.CancelledError:
                    if not prompt_task.done():
                        prompt_task.cancel()
                        with suppress(UpstreamError):
                            await conn.notify("session/cancel", {"sessionId": session_id})
                        with suppress(BaseException):
                            await prompt_task
                    raise
                finally:
                    if not prompt_task.done():
                        prompt_task.cancel()
                        with suppress(BaseException):
                            await prompt_task
