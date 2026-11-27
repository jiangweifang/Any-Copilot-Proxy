from __future__ import annotations

from ..chat_bridge import (
    MAX_OUTPUT_CHARS,
    MAX_PROMPT_CHARS,
    TOOL_CLOSE,
    TOOL_OPEN,
    ChatInput as _BaseChatInput,
    ToolStream as _BaseToolStream,
    collect_chat,
    local_schema,
)

M365_HINT_WARNING = (
    "M365 BizChat exposes no native sampler or token-limit control; "
    "generation preferences are prompt hints, not strict limits."
)


class ChatInput(_BaseChatInput):
    @classmethod
    def parse(cls, payload: dict) -> ChatInput:
        parsed = _BaseChatInput.parse(
            payload,
            channel="M365",
            max_images=4,
            hint_warning=M365_HINT_WARNING,
        )
        return cls(
            parsed.prompt,
            parsed.images,
            parsed.tools,
            parsed.choice,
            parsed.parallel,
            parsed.warnings,
            parsed.reasoning_effort,
        )


class ToolStream(_BaseToolStream):
    def __init__(self, request: _BaseChatInput):
        super().__init__(request, channel="M365")


__all__ = [
    "TOOL_OPEN",
    "TOOL_CLOSE",
    "MAX_PROMPT_CHARS",
    "MAX_OUTPUT_CHARS",
    "local_schema",
    "ChatInput",
    "ToolStream",
    "collect_chat",
]
