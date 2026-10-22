"""ClaudeJudge — 구조화 출력(tool_use 강제)으로 category·confidence·flags를 받는다 (F02-1).

- 입력은 민원 문장(text)뿐이다. customer_id·계좌번호·이름은 이 클래스에 들어오지 않는다 (F02-3).
- 응답이 스키마를 벗어나면 1회 재요청, 그래도 벗어나면 JudgeFormatError(E-LLM-FORMAT).
- API 호출 자체가 실패하면 JudgeError(E-JUDGE). 다른 Judge로 폴백하지 않는다 (F02-4).
- confidence는 모델 자기보고 값이라 보정 전이다. F10 평가에서 ECE를 재고 임계값을 조정한다 (SCRUM-22).
"""
from __future__ import annotations

import os
from typing import Any

from pydantic import ValidationError

from app.judge.base import DEFAULT_TOOLS, Judgment

CATEGORIES = ["TRANSFER_BASIS", "MULTI_BROKER", "TAX_BASIS", "OFFSET", "FX_BASIS", "OUT_OF_SCOPE"]

SYSTEM = """너는 증권사 고객센터의 해외주식 양도소득세 민원 분류기다. 민원 문장 하나를 읽고 classify 도구로 유형을 하나 고른다.

유형 정의 (위에서부터 먼저 해당하는 것을 고른다):
1. TRANSFER_BASIS — 다른 증권사에서 사서 옮긴(이체·대체입고·이관) 주식을 판 경우의 취득가액·취득일·계산서 차이.
   증권사가 두 곳 나와도 주식이 증권사 사이를 옮겨 갔으면 이 유형이다.
   예) "나무증권으로 옮긴 주식 판 뒤에 계산서 취득가액이 이상해요"
   예) "다른 증권사에서 입고받은 주식은 매수 단가가 어떻게 잡히나요"
2. MULTI_BROKER — 여러 증권사에서 각각 거래한 내역을 어떻게 합쳐 신고하는지, 신고대행이 증권사별로 나뉘는 문제, 계산서 합산.
   주식을 옮기지 않고 각 증권사에서 따로 사고판 경우다.
   예) "증권사 두 곳 계산서 합산은 제가 직접 해야 하나요"
   예) "A증권사 신고대행 받았는데 B증권사 거래는 따로 신고해야 하나요"
3. OFFSET — 이익과 손실의 상계(손익통산): 종목 간, 연도 간, 국내·해외 간, 손실이 더 큰 경우.
   예) "손실 본 종목이랑 이익 본 종목 합쳐서 계산하나요"
   예) "손실이 더 크면 세금 안 내도 되나요"
4. FX_BASIS — 환율: 어느 날짜 환율을 쓰는지, 달러 손익과 원화 손익이 다른 이유, 환차익.
   예) "환율 때문에 세금이 늘었다는데 이해가 안 돼요"
   예) "양도가액 환산할 때 어느 날 환율을 쓰나요"
5. TAX_BASIS — 그 밖의 세액·양도차익 계산 자체: 세금이 얼마인지, 앱 수익률과 왜 다른지, 선입선출·이동평균, 기본공제, 필요경비.
   예) "세금이 생각보다 많이 나왔어요 어떻게 계산된 건가요"
   예) "앱 수익이랑 계산서 양도차익이 왜 달라요"
6. OUT_OF_SCOPE — 해외주식 양도소득세 밖: 투자 추천·종목 전망, 배당소득세·국내주식 등 다른 세목, 세금과 무관한 문의.
   예) "지금 엔비디아 사도 될까요" → flags에 ASKS_INVESTMENT_ADVICE
   예) "해외주식 배당금 세금은 어떻게 신고하나요"

flags: 사거나 팔아도 되는지, 어떤 종목이 좋은지 등 투자 판단을 요구하면 ASKS_INVESTMENT_ADVICE. 아니면 빈 배열.
confidence: 고른 유형이 맞을 확률(0~1). 두 유형 사이에서 망설여지면 0.7 아래로, 문장이 모호하거나 세금 질문인지도 불분명하면 0.5 아래로."""

SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": CATEGORIES},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "flags": {"type": "array", "items": {"type": "string", "enum": ["ASKS_INVESTMENT_ADVICE"]}},
    },
    "required": ["category", "confidence", "flags"],
}

RETRY_NOTE = "\n\n직전 응답이 스키마를 벗어났다. category는 enum 중 하나, confidence는 0~1 숫자, flags는 배열로 다시 답하라."


class JudgeError(Exception):
    code = "E-JUDGE"


class JudgeFormatError(JudgeError):
    code = "E-LLM-FORMAT"


class ClaudeJudge:
    name = "claude"

    def __init__(self, model: str | None = None, client: Any = None):
        self.model = model or os.getenv("JUDGE_MODEL") or "claude-haiku-5-5"  # 분류는 가벼운 모델. 에이전트(CLAUDE_MODEL)와 분리
        if client is None:
            from anthropic import AsyncAnthropic
            client = AsyncAnthropic()  # ANTHROPIC_API_KEY
        self.client = client
        self.last_usage = {"calls": 0, "in_tokens": 0, "out_tokens": 0}

    def build_request(self, text: str, retry: bool = False) -> dict:
        """API에 보낼 요청 전체. 여기에 들어가는 고객 정보는 text뿐이다 (테스트로 고정)."""
        return {
            "model": self.model, "max_tokens": 200,  # SDK 2.x는 temperature 인자가 없다
            "system": SYSTEM + (RETRY_NOTE if retry else ""),
            "tools": [{"name": "classify", "description": "민원 유형 분류 결과", "input_schema": SCHEMA}],
            "tool_choice": {"type": "tool", "name": "classify"},
            "messages": [{"role": "user", "content": text}],
        }

    @staticmethod
    def _parse(resp: Any) -> Judgment | None:
        block = next((b for b in resp.content if getattr(b, "type", None) == "tool_use"), None)
        if block is None or not isinstance(block.input, dict):
            return None
        try:
            j = Judgment(**block.input)
        except (ValidationError, TypeError):
            return None
        j.required_tools = list(DEFAULT_TOOLS[j.category])
        return j

    async def classify(self, text: str) -> Judgment:
        self.last_usage = {"calls": 0, "in_tokens": 0, "out_tokens": 0}
        for attempt in range(2):
            try:
                resp = await self.client.messages.create(**self.build_request(text, retry=attempt > 0))
            except Exception as e:  # 네트워크·인증·과금 등. 폴백 없음
                raise JudgeError(f"{type(e).__name__}: {e}") from e
            usage = getattr(resp, "usage", None)
            self.last_usage["calls"] += 1
            self.last_usage["in_tokens"] += getattr(usage, "input_tokens", 0) or 0
            self.last_usage["out_tokens"] += getattr(usage, "output_tokens", 0) or 0
            j = self._parse(resp)
            if j is not None:
                return j
        raise JudgeFormatError("schema violation twice")
