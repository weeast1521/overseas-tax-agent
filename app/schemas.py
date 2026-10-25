"""F01 민원 접수 API 스키마 (기능 명세서 F01, API 명세 요약).

응답은 상담원 콘솔과 평가 러너(F10)가 그대로 읽는다. 여기 모양을 바꾸면 둘 다 바뀐다.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Status = Literal["AUTO", "REVIEW", "REJECTED", "FAILED"]
ReviewReason = Literal["LOW_CONFIDENCE", "METHOD_UNCONFIRMED", "TRANSFER_REVIEW", "VERIFY_FAILED", "AGENT_ABORTED"]
Method = Literal["FIFO", "AVG"]


class InquiryRequest(BaseModel):
    customer_id: int
    tax_year: int
    text: str = Field(min_length=2, max_length=2000)
    method_override: Method | None = None  # 상담원이 고객과 방식을 확정한 뒤 재실행할 때. 저장하지 않는다 (F11-2)
    mode: Literal["sync", "async"] = "sync"  # async는 S2 (F01-5)


class MethodInfo(BaseModel):
    applied: Method | None = None  # 상담원이 확정한 방식. None이면 계좌별 기본 방식
    brokers: dict[str, str] = {}  # account_id → FIFO | AVG | CHOICE (증권사 기본)
    needs_confirmation: list[str] = []  # 선택제인데 고객 선택값이 없는 계좌 (F11-2)


class Source(BaseModel):
    law: str
    article: str
    chunk_id: str | None = None


class InquiryResponse(BaseModel):
    trace_id: str
    status: Status
    review_reason: ReviewReason | None = None
    category: str
    confidence: float
    method: MethodInfo = MethodInfo()
    draft: str | None = None  # 고객에게 읽어 줄 문장 (F07 조립기 customer_text). ANSWER_ASSEMBLER=off면 에이전트 원문
    agent_note: str | None = None  # 상담원만 보는 메모 — 확인할 계좌·이체 lot·내부 오류. 고객 문장과 분리 (F07)
    figures: dict[str, Any] | None = None  # tax_calc CalcResult 그대로 + transferred_lot_sales (F01-4)
    sources: list[Source] = []
    steps: int = 0
    latency_ms: int = 0
    tokens_in: int = 0  # 에이전트 루프 LLM 입력 토큰 합계 (Judge 제외)
    tokens_out: int = 0
    error_code: str | None = None


class ErrorBody(BaseModel):
    error: str
    message: str
    trace_id: str | None = None
