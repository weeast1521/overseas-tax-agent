"""ClaudeJudge — 구조화 출력(tool_use 강제)으로 category·confidence를 받는다.

confidence는 모델 자기보고라 보정 전 값이다. F10 평가에서 ECE를 재고 임계값을 조정한다.
"""
from __future__ import annotations

import os

from anthropic import AsyncAnthropic

from app.judge.base import DEFAULT_TOOLS, Judgment

SYSTEM = """너는 증권사 고객센터의 해외주식 양도소득세 민원 분류기다. 민원 문장 하나를 읽고 유형을 고른다.
- TAX_BASIS: 세금이 얼마인지, 왜 앱 수익률과 다른지 (선입선출·기본공제 설명 필요)
- MULTI_BROKER: 증권사 여러 곳 거래를 어떻게 합치는지, 계산서가 왜 다른지
- OFFSET: 이익·손실 상계(손익통산) 여부
- FX_BASIS: 환율이 언제 기준인지, 달러·원화 손익 차이
- OUT_OF_SCOPE: 세무 범위 밖(투자 추천, 종목 전망, 다른 세목 등)
투자 추천·매수/매도 판단을 요구하면 flags에 ASKS_INVESTMENT_ADVICE를 넣는다.
confidence는 네가 고른 유형이 맞을 확률(0~1)이다. 애매하면 낮게."""

SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": ["TAX_BASIS", "MULTI_BROKER", "OFFSET", "FX_BASIS", "OUT_OF_SCOPE"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "flags": {"type": "array", "items": {"type": "string", "enum": ["ASKS_INVESTMENT_ADVICE"]}},
    },
    "required": ["category", "confidence", "flags"],
}


class ClaudeJudge:
    name = "claude"

    def __init__(self, model: str | None = None):
        self.model = model or os.getenv("JUDGE_MODEL", os.getenv("CLAUDE_MODEL", "claude-sonnet-4-5"))
        self.client = AsyncAnthropic()

    async def classify(self, text: str) -> Judgment:
        resp = await self.client.messages.create(
            model=self.model, max_tokens=200, temperature=0, system=SYSTEM,
            tools=[{"name": "classify", "description": "민원 유형 분류 결과", "input_schema": SCHEMA}],
            tool_choice={"type": "tool", "name": "classify"},
            messages=[{"role": "user", "content": text}],
        )
        block = next(b for b in resp.content if b.type == "tool_use")
        j = Judgment(**block.input)
        j.required_tools = DEFAULT_TOOLS[j.category]
        return j
