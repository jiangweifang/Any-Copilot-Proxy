"""Lightweight fake `agy acp` JSON-RPC 2.0 stdio server for offline tests."""
from __future__ import annotations

import json
import os
import sys


def send(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> int:
    mode = os.environ.get("FAKE_AGY_MODE", "normal")
    models_format = os.environ.get("FAKE_AGY_MODELS_FORMAT", "models")
    support_image = os.environ.get("FAKE_AGY_IMAGE", "1") == "1"
    session_counter = 0
    selected_model = "gemini-2.5-pro"
    permission_outcome = None

    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        if "id" in msg and "method" not in msg:
            # Response to an agent -> client request (e.g. session/request_permission)
            permission_outcome = msg.get("result")
            continue

        req_id = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}

        if method == "initialize":
            send({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": 1,
                    "agentCapabilities": {
                        "promptCapabilities": {"image": support_image},
                    },
                    "authMethods": [
                        {"id": "oauth-personal", "name": "Google Account (Gemini Subscription)"},
                    ],
                },
            })
        elif method == "authenticate":
            if mode == "auth_fail":
                send({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": 401, "message": "Account ineligible or sign-in cancelled"},
                })
            else:
                send({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {"email": "tester@example.com", "methodId": params.get("methodId")},
                })
        elif method == "session/new":
            if mode == "unauth":
                send({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32000, "message": "Unauthenticated: sign in required"},
                })
                continue
            session_counter += 1
            sid = f"sess-{session_counter}"
            if models_format == "configOptions":
                result = {
                    "sessionId": sid,
                    "configOptions": [
                        {
                            "id": "model",
                            "category": "model",
                            "options": [
                                {"value": "gemini-2.5-pro", "name": "Gemini 2.5 Pro"},
                                {"value": "gemini-2.5-flash", "name": "Gemini 2.5 Flash"},
                            ],
                        },
                        {
                            "id": "thought_level",
                            "category": "thought_level",
                            "options": [
                                {"value": "low", "name": "Low"},
                                {"value": "high", "name": "High"},
                            ],
                        },
                    ],
                }
            else:
                result = {
                    "sessionId": sid,
                    "models": {
                        "currentModelId": "gemini-2.5-pro",
                        "availableModels": [
                            {"modelId": "gemini-2.5-pro", "name": "Gemini 2.5 Pro"},
                            {"modelId": "gemini-2.5-flash", "name": "Gemini 2.5 Flash"},
                        ],
                    },
                }
            send({"jsonrpc": "2.0", "id": req_id, "result": result})
        elif method == "session/set_model":
            selected_model = params.get("modelId", selected_model)
            send({"jsonrpc": "2.0", "id": req_id, "result": {}})
        elif method == "session/set_config_option":
            if params.get("configId") == "model":
                selected_model = params.get("value", selected_model)
            send({"jsonrpc": "2.0", "id": req_id, "result": {}})
        elif method == "session/prompt":
            sid = params.get("sessionId")
            blocks = params.get("prompt") or []
            prompt_text = " ".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")
            image_count = sum(1 for b in blocks if isinstance(b, dict) and b.get("type") == "image")

            if mode == "request_permission":
                # Ask client for permission to run a shell tool; client MUST cancel it!
                send({
                    "jsonrpc": "2.0",
                    "id": 9001,
                    "method": "session/request_permission",
                    "params": {"sessionId": sid, "toolCall": {"title": "run_shell"}},
                })
                # Read the client's response synchronously
                reply_line = sys.stdin.readline()
                reply = json.loads(reply_line)
                outcome = ((reply.get("result") or {}).get("outcome") or {}).get("outcome")
                send({
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": sid,
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": f"permission={outcome}"},
                        },
                    },
                })
                send({"jsonrpc": "2.0", "id": req_id, "result": {"stopReason": "end_turn"}})
                continue

            if mode == "tool_call":
                send({
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": sid,
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": '<tool_call>{"name":"get_weather","arguments":{"city":"Tokyo"}}</tool_call>'},
                        },
                    },
                })
                send({"jsonrpc": "2.0", "id": req_id, "result": {"stopReason": "end_turn"}})
                continue

            if mode == "echo_proxy":
                proxy_val = os.environ.get("HTTPS_PROXY", "DIRECT")
                send({
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": sid,
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": f"proxy={proxy_val}"},
                        },
                    },
                })
                send({"jsonrpc": "2.0", "id": req_id, "result": {"stopReason": "end_turn"}})
                continue

            send({
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": sid,
                    "update": {
                        "sessionUpdate": "agent_thought_chunk",
                        "content": {"type": "text", "text": "thinking..."},
                    },
                },
            })
            reply_text = f"[{selected_model}] images={image_count} echo:{prompt_text}"
            send({
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": sid,
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": reply_text},
                    },
                },
            })
            send({"jsonrpc": "2.0", "id": req_id, "result": {"stopReason": "end_turn"}})
        elif method == "session/cancel":
            continue
        elif req_id is not None:
            send({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Unknown method: {method}"}})

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
