"""Claude API 구현체 (개발·검증 구간, 구성 A).

- anthropic SDK 2.x는 temperature 인자를 받지 않는다. 재현성은 평가 러너에서 같은 입력 2회 실행으로 측정한다(F10).
- 데이터 경계(NF, SCRUM-51): 클라이언트는 app/egress 로만 만든다 — 구성 거부 + 송신 전 실데이터 검사. 실데이터는 퍼블릭 API로 나가지 않는다.
"""
from __future__ import annotations

import json
import os

from app import egress
from app.egress import DataBoundaryError, assert_synthetic_data  # noqa: F401  (기존 import 경로 유지)
from app.llm.base import Completion, Message, ToolCall, ToolSpec

DEFAULT_MODEL = "claude-opus-5-5"


class ClaudeClient:
    name = "claude"

    def __init__(self, model: str | None = None, max_tokens: int | None = None):
        self.model = model or os.getenv("CLAUDE_MODEL", DEFAULT_MODEL)
        self.max_tokens = max_tokens or int(os.getenv("CLAUDE_MAX_TOKENS", "1500"))
        self.client = egress.async_anthropic("agent")  # ANTHROPIC_API_KEY. 경계 검사 포함

    @staticmethod
    def to_anthropic(messages: list[Message], tools: list[ToolSpec]) -> dict:
        """내부 Message/ToolSpec → Anthropic Messages API 인자. 테스트에서 payload 검사에 쓴다."""
        system = "\n\n".join(m.content for m in messages if m.role == "system")
        conv: list[dict] = []
        for m in messages:
            if m.role == "system":
                continue
            if m.role == "tool":
                block = {"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.content}
                if conv and conv[-1]["role"] == "user" and isinstance(conv[-1]["content"], list):
                    conv[-1]["content"].append(block)  # 연속 tool_result는 한 user 턴으로
                else:
                    conv.append({"role": "user", "content": [block]})
            elif m.role == "assistant" and m.name:  # assistant가 툴을 부른 턴
                conv.append({"role": "assistant", "content": [{"type": "tool_use", "id": m.tool_call_id, "name": m.name, "input": json.loads(m.content)}]})
            else:
                conv.append({"role": m.role, "content": m.content})
        return {
            "system": system or None,
            "tools": [{"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in tools],
            "messages": conv,
        }

    async def complete(self, messages: list[Message], tools: list[ToolSpec]) -> Completion:
        req = self.to_anthropic(messages, tools)
        if req["system"]:  # 프롬프트 캐시: 시스템 프롬프트(스키마·few-shot·ReAct 지침)는 호출마다 같다 → 캐시 읽기로 TTFT·입력 비용 절감
            req["system"] = [{"type": "text", "text": req["system"], "cache_control": {"type": "ephemeral"}}]
        resp = await self.client.messages.create(model=self.model, max_tokens=self.max_tokens, **req)
        text, call = None, None
        for block in resp.content:
            if block.type == "text":
                text = (text or "") + block.text
            elif block.type == "tool_use":
                call = ToolCall(id=block.id, name=block.name, args=dict(block.input))
        return Completion(text=text, tool_call=call, in_tokens=resp.usage.input_tokens, out_tokens=resp.usage.output_tokens)
