"""Claude API 구현체 (개발·검증 구간, 구성 A).

anthropic SDK 2.x는 temperature 인자를 받지 않는다. 출력 재현성은 평가 러너에서 같은 입력 2회 실행으로 측정한다(F10).
"""
from __future__ import annotations

import os

from anthropic import AsyncAnthropic

from app.llm.base import Completion, Message, ToolCall, ToolSpec


class ClaudeClient:
    name = "claude"

    def __init__(self, model: str | None = None):
        self.model = model or os.getenv("CLAUDE_MODEL", "claude-sonnet-4-5")
        self.client = AsyncAnthropic()  # ANTHROPIC_API_KEY

    async def complete(self, messages: list[Message], tools: list[ToolSpec]) -> Completion:
        system = "\n\n".join(m.content for m in messages if m.role == "system")
        conv = []
        for m in messages:
            if m.role == "system":
                continue
            if m.role == "tool":
                conv.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.content}]})
            elif m.role == "assistant" and m.name:  # assistant가 툴을 부른 턴
                import json
                conv.append({"role": "assistant", "content": [{"type": "tool_use", "id": m.tool_call_id, "name": m.name, "input": json.loads(m.content)}]})
            else:
                conv.append({"role": m.role, "content": m.content})

        resp = await self.client.messages.create(
            model=self.model, max_tokens=1500, system=system or None,
            tools=[{"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in tools],
            messages=conv,
        )
        text, call = None, None
        for block in resp.content:
            if block.type == "text":
                text = (text or "") + block.text
            elif block.type == "tool_use":
                call = ToolCall(id=block.id, name=block.name, args=dict(block.input))
        return Completion(text=text, tool_call=call, in_tokens=resp.usage.input_tokens, out_tokens=resp.usage.output_tokens)
