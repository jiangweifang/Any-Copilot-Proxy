from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
import math
import re
import uuid

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from .network import UpstreamError
from .protocol import ProtocolError

TOOL_OPEN = "<tool_call>"
TOOL_CLOSE = "</tool_call>"
MAX_PROMPT_CHARS = 2 * 1024 * 1024
MAX_OUTPUT_CHARS = 256000


def local_schema(value, channel: str = "M365"):
    if isinstance(value, dict):
        for key, child in value.items():
            if key in ("$ref", "$dynamicRef") and (not isinstance(child, str) or not child.startswith("#")):
                raise ProtocolError(f"{channel} tool schemas support local references only.")
            local_schema(child, channel)
    elif isinstance(value, list):
        for child in value:
            local_schema(child, channel)


@dataclass
class ChatInput:
    prompt: str
    images: list[str]
    tools: dict[str, Draft202012Validator]
    choice: str
    parallel: bool
    warnings: list[str]
    reasoning_effort: str | None = None

    @classmethod
    def parse(
        cls,
        payload: dict,
        *,
        channel: str = "M365",
        max_images: int = 4,
        hint_warning: str | None = None,
    ) -> ChatInput:
        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            raise ProtocolError("messages must be a non-empty array.")
        if payload.get("n", 1) != 1:
            raise ProtocolError(f"{channel} supports n=1 only.")
        for key in ("stop", "logprobs", "top_logprobs", "logit_bias", "seed"):
            if payload.get(key) is not None and payload[key] is not False:
                raise ProtocolError(f"{channel} does not support {key}.")
        if payload.get("response_format") not in (None, {"type": "text"}):
            raise ProtocolError(f"{channel} does not support strict response_format.")
        tools = {}
        definitions = payload.get("tools", [])
        if not isinstance(definitions, list):
            raise ProtocolError("tools must be an array.")
        for definition in definitions:
            function = definition.get("function") if isinstance(definition, dict) else None
            if not isinstance(function, dict) or definition.get("type") != "function":
                raise ProtocolError(f"{channel} supports function tools only.")
            name = function.get("name")
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", name) or name in tools:
                raise ProtocolError("Tool names must be distinct ASCII identifiers.")
            schema = function.get("parameters", {"type": "object"})
            local_schema(schema, channel)
            try:
                Draft202012Validator.check_schema(schema)
            except SchemaError:
                raise ProtocolError("Invalid tool parameter schema.") from None
            tools[name] = Draft202012Validator(schema)
        choice = payload.get("tool_choice", "auto" if tools else "none")
        if isinstance(choice, dict):
            function = choice.get("function")
            if choice.get("type") != "function" or not isinstance(function, dict) or function.get("name") not in tools:
                raise ProtocolError("tool_choice must name a declared function.")
            choice = function["name"]
        elif choice not in ("auto", "none", "required"):
            raise ProtocolError("Unsupported tool_choice.")
        if choice == "required" and not tools:
            raise ProtocolError("tool_choice requires declared tools.")
        parallel = payload.get("parallel_tool_calls", True)
        if type(parallel) is not bool:
            raise ProtocolError("parallel_tool_calls must be a boolean.")
        if choice == "none":
            tools = {}
        transcript, images = [], []
        for message in messages:
            if not isinstance(message, dict) or message.get("role") not in ("system", "developer", "user", "assistant", "tool"):
                raise ProtocolError("Unsupported message role.")
            item = deepcopy(message)
            content = message.get("content")
            if content is None:
                if message["role"] != "assistant" or not message.get("tool_calls"):
                    raise ProtocolError("Message content is required.")
                content = ""
            if isinstance(content, list):
                parts = []
                for part in content:
                    if not isinstance(part, dict):
                        raise ProtocolError("Invalid message content part.")
                    if part.get("type") == "text" and isinstance(part.get("text"), str):
                        parts.append(part["text"])
                    elif part.get("type") == "image_url" and message["role"] == "user":
                        image = part.get("image_url")
                        if not isinstance(image, dict) or not isinstance(image.get("url"), str):
                            raise ProtocolError("image_url requires a URL.")
                        images.append(image["url"])
                        parts.append(f"[Attached image {len(images)}]")
                    else:
                        raise ProtocolError(f"{channel} accepts text and user image_url parts only.")
                content = "\n".join(parts)
            if not isinstance(content, str):
                raise ProtocolError("Message content must be text or a supported content array.")
            if message["role"] == "tool" and not isinstance(message.get("tool_call_id"), str):
                raise ProtocolError("Tool results require tool_call_id.")
            if "tool_calls" in message:
                calls = message["tool_calls"]
                if message["role"] != "assistant" or not isinstance(calls, list):
                    raise ProtocolError("Only assistant messages may contain tool_calls.")
                for call in calls:
                    function = call.get("function") if isinstance(call, dict) else None
                    if (not isinstance(function, dict) or call.get("type") != "function" or
                            not isinstance(call.get("id"), str) or not isinstance(function.get("name"), str) or
                            not isinstance(function.get("arguments"), str)):
                        raise ProtocolError("Invalid assistant tool-call history.")
            item["content"] = content
            transcript.append(item)
        if len(images) > max_images:
            raise ProtocolError(f"{channel} supports at most {max_images} images per request.")
        if len(transcript) == 1 and transcript[0]["role"] == "user" and not tools:
            prompt = transcript[0]["content"]
        else:
            prompt = ("Continue this conversation and answer its latest user request. "
                      "The JSON records preserve the supplied roles and function results.\n" +
                      json.dumps(transcript, ensure_ascii=False))
        if tools:
            offered = [{"name": definition["function"]["name"],
                        "description": definition["function"].get("description", ""),
                        "parameters": definition["function"].get("parameters", {"type": "object"})}
                       for definition in definitions]
            prompt += (
                "\n\nThis client can execute the listed functions and return their results to you. "
                "When a function is useful, request it using this exact text format:\n"
                '<tool_call>{"name":"function_name","arguments":{"parameter":"value"}}</tool_call>\n'
                "Use a declared function name and a valid JSON argument object matching its schema. "
                "Do not wrap function requests in Markdown. Wait for the client's result rather than "
                "claiming to have executed a function yourself. Otherwise respond normally.\n"
                "Available functions:\n" + json.dumps(offered, ensure_ascii=False))
            if not parallel:
                prompt += "\nRequest at most one function in this reply."
            if choice == "required":
                prompt += "\nFor this reply, request a listed function."
            elif choice in tools:
                prompt += f"\nFor this reply, request the function named {choice}."
        hints = {key: payload[key] for key in ("max_tokens", "max_completion_tokens", "temperature", "top_p",
                                             "presence_penalty", "frequency_penalty", "reasoning_effort")
                 if payload.get(key) is not None}
        warnings = []
        reasoning_effort = None
        if hints:
            for key, value in hints.items():
                if key == "reasoning_effort":
                    if value not in ("none", "minimal", "low", "medium", "high", "xhigh", "max"):
                        raise ProtocolError("Invalid reasoning_effort.")
                    reasoning_effort = value
                elif type(value) not in (int, float) or not math.isfinite(value):
                    raise ProtocolError(f"{key} must be a number.")
                if key in ("max_tokens", "max_completion_tokens") and (type(value) is not int or value < 1):
                    raise ProtocolError(f"{key} must be a positive integer.")
            prompt += "\n\nRequested response preferences: " + json.dumps(hints) + "."
            warnings.append(
                hint_warning or
                f"{channel} exposes no native sampler or token-limit control; generation preferences are prompt hints, not strict limits."
            )
        if len(prompt) > MAX_PROMPT_CHARS:
            raise ProtocolError(f"{channel} prompt exceeds the adapter's 2 Mi-character limit; reduce history or tool descriptions.")
        return cls(prompt, images, tools, choice, parallel, warnings, reasoning_effort)


class ToolStream:
    def __init__(self, request: ChatInput, *, channel: str = "M365"):
        self.request = request
        self.channel = channel
        self.buffer = ""
        self.collecting = False
        self.calls = 0
        self.received = 0

    def push(self, text: str) -> list[dict]:
        self.received += len(text)
        if self.received > MAX_OUTPUT_CHARS:
            raise UpstreamError(502, f"{self.channel} output exceeded the adapter limit.")
        if not self.request.tools:
            return [{"content": text}] if text else []
        self.buffer += text
        result = []
        while self.buffer:
            if self.collecting:
                end = self.buffer.find(TOOL_CLOSE)
                if end < 0:
                    if len(self.buffer) > 65536:
                        raise UpstreamError(502, f"{self.channel} tool arguments exceed 64 KiB.")
                    break
                raw, self.buffer = self.buffer[:end], self.buffer[end + len(TOOL_CLOSE):]
                try:
                    call = json.loads(raw)
                except (ValueError, RecursionError):
                    raise UpstreamError(502, f"{self.channel} returned malformed tool-call JSON.") from None
                if not isinstance(call, dict) or set(call) != {"name", "arguments"}:
                    raise UpstreamError(502, f"{self.channel} returned an invalid tool call.")
                name, arguments = call["name"], call["arguments"]
                if not isinstance(name, str) or name not in self.request.tools or not isinstance(arguments, dict):
                    raise UpstreamError(502, f"{self.channel} requested an undeclared tool or invalid arguments.")
                if self.request.choice not in ("auto", "required") and name != self.request.choice:
                    raise UpstreamError(502, f"{self.channel} did not honor the selected function.")
                if self.calls and not self.request.parallel:
                    raise UpstreamError(502, f"{self.channel} returned multiple calls when parallel_tool_calls is false.")
                try:
                    self.request.tools[name].validate(arguments)
                except ValidationError:
                    raise UpstreamError(502, f"{self.channel} tool arguments do not match the declared schema.") from None
                result.append({"tool_calls": [{"index": self.calls, "id": "call_" + uuid.uuid4().hex,
                                              "type": "function", "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}]})
                self.calls += 1
                self.collecting = False
            else:
                start = self.buffer.find(TOOL_OPEN)
                if start >= 0:
                    if start:
                        result.append({"content": self.buffer[:start]})
                    self.buffer = self.buffer[start + len(TOOL_OPEN):]
                    self.collecting = True
                else:
                    keep = 0
                    for length in range(1, min(len(self.buffer), len(TOOL_OPEN) - 1) + 1):
                        if TOOL_OPEN.startswith(self.buffer[-length:]):
                            keep = length
                    text = self.buffer[:-keep] if keep else self.buffer
                    if text:
                        result.append({"content": text})
                    self.buffer = self.buffer[-keep:] if keep else ""
                    break
        return result

    def finish(self) -> list[dict]:
        if self.collecting or (self.buffer and TOOL_OPEN.startswith(self.buffer) and len(self.buffer) > 2):
            raise UpstreamError(502, f"{self.channel} ended with an incomplete tool call.")
        if self.request.choice not in ("none", "auto") and not self.calls:
            raise UpstreamError(502, f"{self.channel} completed without the required function call.")
        result = [{"content": self.buffer}] if self.buffer else []
        self.buffer = ""
        return result


def collect_chat(chunks: list[dict], *, channel: str = "M365") -> dict:
    if not chunks or chunks[-1]["choices"][0]["finish_reason"] is None:
        raise UpstreamError(502, f"{channel} stream did not complete.")
    content = ""
    reasoning = ""
    calls = {}
    for chunk in chunks:
        delta = chunk["choices"][0]["delta"]
        content += delta.get("content", "")
        reasoning += delta.get("reasoning_content", "")
        for call in delta.get("tool_calls", []):
            calls[call["index"]] = {key: value for key, value in call.items() if key != "index"}
    first = chunks[0]
    message = {"role": "assistant", "content": content or None}
    if reasoning:
        message["reasoning_content"] = reasoning
    if calls:
        message["tool_calls"] = [calls[index] for index in sorted(calls)]
    result = {"id": first["id"], "object": "chat.completion", "created": first["created"], "model": first["model"],
              "choices": [{"index": 0, "message": message, "finish_reason": chunks[-1]["choices"][0]["finish_reason"]}]}
    if first.get("warnings"):
        result["warnings"] = first["warnings"]
    return result
