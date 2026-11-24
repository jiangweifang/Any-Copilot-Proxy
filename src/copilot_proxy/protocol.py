from __future__ import annotations

from copy import deepcopy
import json
import time
import uuid


class ProtocolError(ValueError):
    pass


def content_parts(content, role):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise ProtocolError("Message content must be text or an array.")
    parts = []
    for part in content:
        match part.get("type"):
            case "text":
                parts.append({"type": "output_text" if role == "assistant" else "input_text", "text": part["text"]})
            case "image_url":
                parts.append({"type": "input_image", "image_url": part["image_url"]["url"], "detail": part["image_url"].get("detail", "auto")})
            case "file":
                parts.append({"type": "input_file", **part["file"]})
            case _:
                raise ProtocolError("Unsupported message content type.")
    return parts


def to_responses(payload: dict) -> dict:
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ProtocolError("messages is required")
    if payload.get("n", 1) != 1:
        raise ProtocolError("Responses models support n=1 only")
    if payload.get("stop") is not None:
        raise ProtocolError("Responses models do not support stop sequences")
    items = []
    for message in messages:
        role = message.get("role")
        if role == "tool":
            if not message.get("tool_call_id"):
                raise ProtocolError("Tool messages require tool_call_id")
            content = message.get("content", "")
            items.append({"type": "function_call_output", "call_id": message["tool_call_id"],
                          "output": content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)})
            continue
        if role not in ("user", "assistant", "system", "developer"):
            raise ProtocolError("Unsupported message role")
        if message.get("content") not in (None, ""):
            items.append({"role": role, "content": content_parts(message["content"], role)})
        for call in message.get("tool_calls", []):
            if call.get("type") != "function":
                raise ProtocolError("Only function tools are supported")
            items.append({"type": "function_call", "call_id": call["id"], "name": call["function"]["name"],
                          "arguments": call["function"]["arguments"]})
    result = {"model": payload["model"], "input": items, "store": False, "stream": payload.get("stream", False)}
    maximum = payload.get("max_completion_tokens", payload.get("max_tokens"))
    if maximum is not None:
        result["max_output_tokens"] = maximum
    for key in ("temperature", "top_p", "parallel_tool_calls"):
        if payload.get(key) is not None:
            result[key] = payload[key]
    if payload.get("reasoning_effort") is not None:
        result["reasoning"] = {"effort": payload["reasoning_effort"]}
    if "tools" in payload:
        result["tools"] = []
        for tool in payload["tools"]:
            if tool.get("type") != "function":
                raise ProtocolError("Only function tools are supported")
            result["tools"].append({"type": "function", **deepcopy(tool["function"]), "strict": tool["function"].get("strict", False)})
    if "tool_choice" in payload:
        choice = payload["tool_choice"]
        result["tool_choice"] = choice if isinstance(choice, str) else {"type": "function", "name": choice["function"]["name"]}
    if "response_format" in payload:
        fmt = payload["response_format"]
        if fmt["type"] == "json_schema":
            result["text"] = {"format": {"type": "json_schema", **deepcopy(fmt["json_schema"])}}
        elif fmt["type"] in ("json_object", "text"):
            result["text"] = {"format": deepcopy(fmt)}
        else:
            raise ProtocolError("Unsupported response_format")
    return result


def usage_for_chat(usage):
    if usage is None:
        return None
    source, output = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
    result = {"prompt_tokens": source, "completion_tokens": output, "total_tokens": usage.get("total_tokens", source + output)}
    for source_key, target_key in (("input_tokens_details", "prompt_tokens_details"), ("output_tokens_details", "completion_tokens_details")):
        if source_key in usage:
            result[target_key] = usage[source_key]
    return result


def check_response(response):
    if response.get("status") == "failed" or response.get("error"):
        raise RuntimeError(response.get("error", {}).get("message", "Upstream response failed"))


def to_chat(response: dict, model: str) -> dict:
    check_response(response)
    content = reasoning = refusal = ""
    calls = []
    for item in response.get("output", []):
        if item["type"] == "message":
            for part in item.get("content", []):
                if part["type"] == "output_text":
                    content += part["text"]
                elif part["type"] == "refusal":
                    refusal += part["refusal"]
        elif item["type"] == "reasoning":
            reasoning += "".join(p.get("text", "") for p in item.get("summary", []))
        elif item["type"] == "function_call":
            calls.append({"id": item["call_id"], "type": "function", "function": {"name": item["name"], "arguments": item["arguments"]}})
    message = {"role": "assistant", "content": content or None}
    if calls:
        message["tool_calls"] = calls
    if reasoning:
        message["reasoning_content"] = reasoning
    if refusal:
        message["refusal"] = refusal
    return {"id": "chatcmpl-" + response.get("id", uuid.uuid4().hex), "object": "chat.completion",
            "created": response.get("created_at", int(time.time())), "model": response.get("model", model),
            "choices": [{"index": 0, "message": message,
                         "finish_reason": "length" if response.get("status") == "incomplete" else "tool_calls" if calls else "stop"}],
            "usage": usage_for_chat(response.get("usage"))}


class ResponseStream:
    def __init__(self, model: str):
        self.model = model
        self.id = "chatcmpl-" + uuid.uuid4().hex
        self.created = int(time.time())
        self.calls = {}
        self.completed = False

    def chunk(self, delta: dict, finish=None, usage=None) -> dict:
        result = {"id": self.id, "object": "chat.completion.chunk", "created": self.created, "model": self.model,
                  "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        if usage is not None:
            result["usage"] = usage_for_chat(usage)
        return result

    def convert(self, event: dict) -> dict | None:
        kind = event.get("type")
        for types, key in ((("response.output_text.delta",), "content"),
                           (("response.reasoning_summary_text.delta", "response.reasoning_text.delta"), "reasoning_content"),
                           (("response.refusal.delta",), "refusal")):
            if kind in types:
                return self.chunk({key: event["delta"]})
        if kind == "response.output_item.added" and event["item"]["type"] == "function_call":
            item = event["item"]
            call = {"index": len(self.calls), "has_arguments": bool(item.get("arguments"))}
            self.calls[event["output_index"]] = call
            return self.chunk({"tool_calls": [{"index": call["index"], "id": item["call_id"], "type": "function",
                                               "function": {"name": item["name"], "arguments": item.get("arguments", "")}}]})
        if kind == "response.function_call_arguments.delta" or (kind == "response.output_item.done" and event["item"]["type"] == "function_call"):
            call = self.calls.get(event["output_index"])
            if call is None:
                raise RuntimeError("Tool arguments received without a tool call")
            arguments = event["delta"] if kind.endswith(".delta") else event["item"].get("arguments", "") if not call["has_arguments"] else ""
            if arguments:
                call["has_arguments"] = True
                return self.chunk({"tool_calls": [{"index": call["index"], "function": {"arguments": arguments}}]})
        if kind in ("response.completed", "response.incomplete"):
            check_response(event["response"])
            self.completed = True
            return self.chunk({}, "length" if kind == "response.incomplete" else "tool_calls" if self.calls else "stop", event["response"].get("usage"))
        if kind in ("response.failed", "error"):
            raise RuntimeError(event.get("response", {}).get("error", {}).get("message") or event.get("message", "Upstream stream failed"))
        return None


async def read_events(stream):
    data = []
    size = 0
    async for raw in stream:
        line = raw.decode("utf-8").rstrip("\r\n")
        if not line:
            if data:
                yield "\n".join(data)
            data, size = [], 0
        elif line.startswith("data:"):
            value = line[5:].removeprefix(" ")
            size += len(raw)
            if size > 16 * 1024 * 1024:
                raise RuntimeError("Upstream SSE event exceeds 16 MiB")
            data.append(value)
    if data:
        yield "\n".join(data)
