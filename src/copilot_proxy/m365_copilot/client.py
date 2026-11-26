from __future__ import annotations

import asyncio
import base64
import binascii
from dataclasses import replace
from datetime import datetime, timezone
import ipaddress
import json
import logging
import re
import socket
from urllib.parse import quote, urlsplit
import uuid

import aiohttp

from .auth import M365Profile, probe_models
from .protocol import ChatInput, ToolStream
from ..network import Network, UpstreamError
from ..protocol import ProtocolError, ResponseStream

log = logging.getLogger(__name__)
RS = "\x1e"
MAX_IMAGE_BYTES = 10 * 1024 * 1024
IMAGE_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp", "image/bmp": "bmp"}


class PublicImageResolver(aiohttp.resolver.DefaultResolver):
    async def resolve(self, host, port=0, family=socket.AF_INET):
        addresses = await super().resolve(host, port, family)
        if not addresses or any(not ipaddress.ip_address(address["host"]).is_global for address in addresses):
            raise ProtocolError("Remote images must resolve exclusively to public IP addresses.")
        return addresses


async def read_json(response, limit=1024 * 1024) -> dict:
    body = bytearray()
    async for chunk in response.content.iter_chunked(16384):
        body.extend(chunk)
        if len(body) > limit:
            raise UpstreamError(502, "M365 response exceeded its size limit.")
    try:
        result = json.loads(body)
    except (ValueError, UnicodeError):
        raise UpstreamError(502, "M365 returned invalid JSON.") from None
    if not isinstance(result, dict):
        raise UpstreamError(502, "M365 returned an invalid object.")
    return result


def image_type(data: bytes, mime: str) -> str:
    matches = {"image/png": data.startswith(b"\x89PNG\r\n\x1a\n"), "image/jpeg": data.startswith(b"\xff\xd8\xff"),
               "image/gif": data.startswith((b"GIF87a", b"GIF89a")),
               "image/webp": data.startswith(b"RIFF") and data[8:12] == b"WEBP", "image/bmp": data.startswith(b"BM")}
    if not data or len(data) > MAX_IMAGE_BYTES or not matches.get(mime):
        raise ProtocolError("Images must be valid PNG, JPEG, GIF, WebP or BMP data of at most 10 MiB.")
    return mime


class FrameDecoder:
    def __init__(self, forced_model: bool):
        self.messages: dict[str, str] = {}
        self.current = ""
        self.forced_model = forced_model
        self.done = False

    def consume(self, frame: dict) -> list[str]:
        kind = frame.get("type")
        if frame.get("error"):
            raise UpstreamError(502, "M365 rejected the chat invocation.")
        if kind in (3, 7):
            if kind == 7 or frame.get("invocationId", "0") == "0":
                self.done = True
            return []
        payloads = frame.get("arguments", [frame["item"]] if "item" in frame else [])
        if not isinstance(payloads, list):
            raise UpstreamError(502, "M365 returned invalid SignalR arguments.")
        output = []
        for payload in payloads:
            if not isinstance(payload, dict):
                continue
            result = payload.get("result")
            if isinstance(result, dict) and result.get("value") not in (None, "Success"):
                code = result.get("errorCode") or result.get("value")
                code = code if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", code) else "RequestFailed"
                status = 429 if any(word in code.lower() for word in ("throttl", "credit", "quota")) else 403 if "license" in code.lower() else 502
                raise UpstreamError(status, f"M365 rejected the request: {code}.")
            delta = payload.get("writeAtCursor")
            if isinstance(delta, str) and delta:
                self.messages[self.current] = self.messages.get(self.current, "") + delta
                output.append(delta)
            messages = payload.get("messages", [])
            if not isinstance(messages, list):
                raise UpstreamError(502, "M365 returned invalid messages.")
            for message in messages:
                if not isinstance(message, dict) or message.get("author") != "bot":
                    continue
                message_type = message.get("messageType")
                if message_type == "Disengaged":
                    raise UpstreamError(403, "M365 declined the request.")
                if message_type in ("EndOfRequest", "RenderCardRequest"):
                    self.done = True
                    continue
                if message_type not in (None, "Chat"):
                    continue
                text = message.get("text")
                if not isinstance(text, str) or not text:
                    continue
                if self.forced_model and not any(self.messages.values()) and message.get("contentOrigin") == "BotConnection":
                    raise UpstreamError(503, "This M365 model is not available for the current account or scenario.")
                identity = message.get("messageId", self.current)
                if not isinstance(identity, str):
                    raise UpstreamError(502, "M365 returned an invalid message identity.")
                previous = self.messages.get(identity)
                if previous is None and not self.current and text.startswith(self.messages.get("", "")):
                    previous = self.messages.pop("", "")
                previous = previous or ""
                if not text.startswith(previous):
                    raise UpstreamError(502, "M365 revised text already emitted in the stream.")
                if len(text) > len(previous):
                    output.append(text[len(previous):])
                self.current = identity
                self.messages[identity] = text
        return output


class M365Client:
    def __init__(self, network: Network):
        self.network = network
        self.network.websocket_hosts = ("substrate.office.com", "substrate.svc.cloud.microsoft")

    async def models(self, profile: M365Profile) -> list[dict]:
        scenarios = [profile.scenario] if profile.kind == "personal" else ["OfficeWebIncludedCopilot", "OfficeWebPaidCopilot"]
        models, failures = [], []
        for scenario in scenarios:
            try:
                menu = await probe_models(self.network, replace(profile, scenario=scenario))
            except UpstreamError as error:
                if error.status == 401:
                    raise
                log.warning("M365 catalog for %s failed: HTTP %s.", scenario, error.status)
                failures.append(error)
                continue
            for model in menu:
                suffix = "@paid" if scenario == "OfficeWebPaidCopilot" else ""
                models.append({**model, "id": f"m365/{model['id']}{suffix}", "tone": model["id"], "scenario": scenario,
                               "owned_by": "M365 Copilot", "supported_endpoints": ["/chat/completions"]})
        if not models:
            raise failures[0] if failures else UpstreamError(503, "No M365 chat models are available.")
        return models

    async def load_image(self, value: str) -> tuple[str, bytes]:
        if value.startswith("data:"):
            match = re.fullmatch(r"data:(image/[a-zA-Z0-9.+-]+);base64,([A-Za-z0-9+/=\r\n]+)", value)
            if not match:
                raise ProtocolError("M365 image data must use a base64 data URL.")
            mime = match[1].lower().replace("image/jpg", "image/jpeg")
            try:
                data = base64.b64decode(match[2].replace("\n", "").replace("\r", ""), validate=True)
            except binascii.Error:
                raise ProtocolError("Invalid image base64 data.") from None
        else:
            try:
                uri = urlsplit(value)
                valid = uri.scheme == "https" and uri.hostname and not uri.username and not uri.password and uri.port in (None, 443)
            except ValueError:
                valid = False
            if not valid:
                raise ProtocolError("Remote images require a public HTTPS URL without credentials.")
            try:
                address = ipaddress.ip_address(uri.hostname)
            except ValueError:
                address = None
            if address is not None and not address.is_global:
                raise ProtocolError("Remote images must use public IP addresses.")
            if self.network.settings.type != "none":
                raise ProtocolError("With an upstream proxy, supply image data URLs instead of remote image URLs.")
            connector = aiohttp.TCPConnector(resolver=PublicImageResolver())
            async with aiohttp.ClientSession(connector=connector, trust_env=False, timeout=aiohttp.ClientTimeout(total=30)) as session:
                async with session.get(value, allow_redirects=False) as response:
                    if response.status != 200:
                        raise UpstreamError(502, f"Remote image returned HTTP {response.status}; redirects are not followed.")
                    mime = response.content_type
                    body = bytearray()
                    async for chunk in response.content.iter_chunked(16384):
                        body.extend(chunk)
                        if len(body) > MAX_IMAGE_BYTES:
                            raise ProtocolError("Image exceeds 10 MiB.")
                    data = bytes(body)
        return image_type(data, mime), data

    async def upload_images(self, profile: M365Profile, scenario: str, conversation: str, images: list[str]) -> list[dict]:
        annotations = []
        for value in images:
            mime, data = await self.load_image(value)
            fields = [("scenario", "UploadImage"), ("conversationId", conversation),
                      ("FileBase64", f"data:{mime};base64," + base64.b64encode(data).decode()),
                      ("optionsSets", "cwcgptvsan"), ("optionsSets", "flux_v3_gptv_enable_upload_multi_image_in_turn_wo_ch")]
            headers = {"Authorization": f"Bearer {profile.access_token}", "Origin": profile.origin,
                       "Referer": profile.origin + "/", "User-Agent": profile.user_agent, "X-Scenario": scenario,
                       "X-Variants": "feature.EnableImageSupportInUploadFile"}
            async with self.network.request("POST", "https://substrate.office.com/m365Copilot/UploadFile",
                                            headers=headers, data=fields) as response:
                if response.status >= 300:
                    raise UpstreamError(response.status, f"M365 image upload returned HTTP {response.status}.")
                result = await read_json(response)
            doc_id = result.get("docId")
            if not isinstance(doc_id, str) or not 1 <= len(doc_id) <= 1024:
                raise UpstreamError(502, "M365 image upload did not return a document ID.")
            extension = IMAGE_TYPES[mime]
            annotations.append({"id": doc_id, "messageAnnotationType": "ImageFile",
                                "messageAnnotationMetadata": {"@type": "File", "annotationType": "File",
                                                              "fileType": extension, "fileName": f"image-{len(annotations) + 1}.{extension}"}})
        return annotations

    async def chat(self, profile: M365Profile, model: dict, payload: dict):
        request = ChatInput.parse(payload)
        conversation, session_id = str(uuid.uuid4()), str(uuid.uuid4())
        scenario, tone = model["scenario"], model["tone"]
        params = {"access_token": profile.access_token, "ConversationId": conversation,
                  "chatsessionid": session_id, "clientrequestid": session_id, "X-SessionId": session_id,
                  "source": "officeweb", "product": "Office", "agentHost": "Bizchat.FullScreen",
                  "scenario": scenario, "disableMemory": "1"}
        if scenario != "OfficeWebIncludedCopilot":
            params["licenseType"] = "Premium"
        url = f"wss://substrate.office.com/m365Copilot/Chathub/{quote(profile.oid, safe='')}@{quote(profile.tid, safe='')}"
        headers = {"Origin": profile.origin, "User-Agent": profile.user_agent, "X-RoutingParameter-SessionKey": str(uuid.uuid4())}
        stream, parser = ResponseStream(payload["model"]), ToolStream(request)
        decoder = FrameDecoder(forced_model=tone.lower() != "magic")
        for warning in request.warnings:
            log.warning("%s", warning)
        async with asyncio.timeout(300):
            annotations = await self.upload_images(profile, scenario, conversation, request.images)
            try:
                async with self.network.websocket(url, params=params, headers=headers) as websocket:
                    buffer, acknowledged, sent = "", False, False
                    try:
                        await websocket.send_str(json.dumps({"protocol": "json", "version": 1}) + RS)
                        while not decoder.done:
                            message = await websocket.receive(timeout=90 if acknowledged else 15)
                            if message.type != aiohttp.WSMsgType.TEXT:
                                raise UpstreamError(502, "M365 WebSocket closed without a completed response.")
                            buffer += message.data
                            if len(buffer) > 16 * 1024 * 1024:
                                raise UpstreamError(502, "M365 SignalR frame exceeds 16 MiB.")
                            while RS in buffer:
                                raw, buffer = buffer.split(RS, 1)
                                if not raw:
                                    continue
                                try:
                                    frame = json.loads(raw)
                                except ValueError:
                                    raise UpstreamError(502, "M365 returned invalid SignalR JSON.") from None
                                if not isinstance(frame, dict):
                                    raise UpstreamError(502, "M365 returned an invalid SignalR frame.")
                                if not acknowledged:
                                    if frame != {}:
                                        raise UpstreamError(502, "M365 SignalR handshake was rejected.")
                                    acknowledged = True
                                    args = {"source": "officeweb", "tone": "magic" if tone.lower() == "magic" else tone,
                                            "streamingMode": "ConciseWithPadding", "isStartOfSession": True,
                                            "conversationId": conversation,
                                            "allowedMessageTypes": ["Chat", "Suggestion", "Progress", "Disengaged", "EndOfRequest"],
                                            "clientInfo": {"clientPlatform": "mcmcopilot-web", "clientAppName": "Office"},
                                            "message": {"author": "user", "messageType": "Chat", "locale": "en-US", "text": request.prompt}}
                                    if annotations:
                                        args["message"]["messageAnnotations"] = annotations
                                    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                                    metrics = {"type": 1, "target": "Metrics", "arguments": [{"Timestamps": {
                                        name: now for name in ("ConnectionStart", "ConnectionEstablished", "UserInputStart", "UserInputSubmit")}}]}
                                    invocation = {"type": 4, "target": "chat", "invocationId": "0", "arguments": [args]}
                                    await websocket.send_str(json.dumps(invocation) + RS + json.dumps(metrics) + RS)
                                    sent = True
                                    first = stream.chunk({"role": "assistant", "content": ""})
                                    if request.warnings:
                                        first["warnings"] = request.warnings
                                    yield first
                                    continue
                                if frame.get("type") == 6:
                                    await websocket.send_str(json.dumps({"type": 6}) + RS)
                                    continue
                                for text in decoder.consume(frame):
                                    for delta in parser.push(text):
                                        yield stream.chunk(delta)
                                if decoder.done:
                                    break
                        for delta in parser.finish():
                            yield stream.chunk(delta)
                        if not parser.received:
                            raise UpstreamError(502, "M365 completed without an assistant response.")
                        yield stream.chunk({}, "tool_calls" if parser.calls else "stop")
                    finally:
                        if sent and not decoder.done and not websocket.closed:
                            try:
                                async with asyncio.timeout(2):
                                    await websocket.send_str(json.dumps({"type": 1, "target": "stop", "invocationId": "1", "arguments": [{}]}) + RS)
                            except (aiohttp.ClientError, ConnectionError, TimeoutError):
                                log.warning("M365 cancellation could not be acknowledged; closing the socket.")
            except aiohttp.WSServerHandshakeError as error:
                raise UpstreamError(error.status, f"M365 WebSocket authorization returned HTTP {error.status}.") from None
