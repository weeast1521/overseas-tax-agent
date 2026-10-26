"""F02 Judge — 민원 문장 → 유형·확신도·필요 툴. 구현체는 설정으로 교체.

구현체: ClaudeJudge(1차), JevJudge(얼리 액세스 승인 후), LocalJudge(폐쇄망, 6일차).
Judge 실패 시 다른 구현체로 폴백하지 않는다 (F02-4).
"""
from __future__ import annotations

import os
from typing import Literal, Protocol

from pydantic import BaseModel, Field

Category = Literal["TRANSFER_BASIS", "MULTI_BROKER", "TAX_BASIS", "OFFSET", "FX_BASIS", "FILING", "OUT_OF_SCOPE"]

# broker_method는 넣지 않는다 (SCRUM-19, 10/26): 산정 방식은 코드(resolve_methods)가 정해 계산·조립에 직접 넣으므로
# 에이전트가 물어볼 필요가 없다. 여기 있으면 에이전트가 매번 먼저 불러 건당 3.3초를 썼다(홀드아웃 30건 중 24건).
DEFAULT_TOOLS: dict[str, list[str]] = {
    "TRANSFER_BASIS": ["sql_query", "tax_calc", "reg_search"],  # 이체(대체입고) 취득가액
    "MULTI_BROKER": ["sql_query", "tax_calc", "reg_search"],
    "TAX_BASIS": ["sql_query", "tax_calc", "reg_search"],
    "OFFSET": ["sql_query", "tax_calc", "reg_search"],
    "FX_BASIS": ["sql_query", "reg_search"],  # 환율 기준일 설명이 중심. 숫자는 figures(직접 계산)로 나간다
    "FILING": ["reg_search"],  # 신고 절차·과세 대상 여부 — 규정 설명만, 계산 없음 (범위 결정 (a), 10/24)
    "OUT_OF_SCOPE": [],
}

REVIEW_THRESHOLD = float(os.getenv("JUDGE_REVIEW_THRESHOLD", "0.70"))
# 임계값 근거 (SCRUM-22, 10/24): 합성 200문장 × Haiku Judge 5회 = 1,000판정에서 confidence ≥ 0.70 구간 분류 오답 0,
# 흔들림 표준편차 0.012. 0.95는 AUTO 8%뿐(사실상 전부 사람 검토). 0.85 → AUTO 91%, 오답 0. 실데이터로 재확인 후 0.80 검토.
AUTO_THRESHOLD = float(os.getenv("JUDGE_AUTO_THRESHOLD", "0.85"))


class Judgment(BaseModel):
    category: Category
    confidence: float = Field(ge=0, le=1)
    required_tools: list[str] = []
    flags: list[str] = []

    def gate(self) -> Literal["REJECTED", "REVIEW", "RUN_REVIEW", "RUN_AUTO"]:
        if self.category == "OUT_OF_SCOPE" or "ASKS_INVESTMENT_ADVICE" in self.flags:
            return "REJECTED"
        if self.confidence < REVIEW_THRESHOLD:
            return "REVIEW"
        return "RUN_AUTO" if self.confidence >= AUTO_THRESHOLD else "RUN_REVIEW"


class Judge(Protocol):
    name: str

    async def classify(self, text: str) -> Judgment: ...


def get_judge() -> Judge:
    impl = os.getenv("JUDGE_IMPL", "claude")
    if impl == "claude":
        from app.judge.claude_judge import ClaudeJudge
        return ClaudeJudge()
    if impl == "rule":
        from app.judge.rule_judge import RuleJudge
        return RuleJudge()
    raise ValueError(f"unknown JUDGE_IMPL={impl}")
