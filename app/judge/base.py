"""F02 Judge — 민원 문장 → 유형·확신도·필요 툴. 구현체는 설정으로 교체.

구현체: ClaudeJudge(1차), JevJudge(얼리 액세스 승인 후), LocalJudge(폐쇄망, 6일차).
Judge 실패 시 다른 구현체로 폴백하지 않는다 (F02-4).
"""
from __future__ import annotations

import os
from typing import Literal, Protocol

from pydantic import BaseModel, Field

Category = Literal["TRANSFER_BASIS", "MULTI_BROKER", "TAX_BASIS", "OFFSET", "FX_BASIS", "OUT_OF_SCOPE"]

DEFAULT_TOOLS: dict[str, list[str]] = {
    "TRANSFER_BASIS": ["sql_query", "broker_method", "tax_calc", "reg_search"],  # 이체(대체입고) 취득가액
    "MULTI_BROKER": ["sql_query", "broker_method", "tax_calc", "reg_search"],
    "TAX_BASIS": ["sql_query", "broker_method", "tax_calc", "reg_search"],
    "OFFSET": ["sql_query", "tax_calc", "reg_search"],
    "FX_BASIS": ["sql_query", "reg_search"],  # 환율 기준일 설명이 중심. 숫자는 figures(직접 계산)로 나간다
    "OUT_OF_SCOPE": [],
}

REVIEW_THRESHOLD = float(os.getenv("JUDGE_REVIEW_THRESHOLD", "0.70"))
AUTO_THRESHOLD = float(os.getenv("JUDGE_AUTO_THRESHOLD", "0.95"))


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
