"""F01 민원 접수 API (FastAPI). 1일차 뼈대: /v1/inquiries, /v1/tools/tax_calc, /healthz."""
from __future__ import annotations

import os
import time
import uuid
from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from app.agent import react
from app.judge.base import get_judge
from app.llm.base import get_client
from app.tools import registry, tax_calc

TAX_YEAR = int(os.getenv("TAX_YEAR", "2025"))

app = FastAPI(title="tax-agent", version="0.1.0")


class InquiryIn(BaseModel):
    customer_id: int
    tax_year: int = TAX_YEAR
    text: str = Field(min_length=2, max_length=2000)
    mode: Literal["sync", "async"] = "sync"


class InquiryOut(BaseModel):
    trace_id: str
    status: Literal["AUTO", "REVIEW", "REJECTED", "FAILED"]
    category: str
    confidence: float
    draft: str | None = None
    steps: int = 0
    latency_ms: int = 0
    error_code: str | None = None


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.post("/v1/tools/tax_calc", response_model=tax_calc.CalcResult)
async def tool_tax_calc(req: tax_calc.CalcRequest):
    """디버그·내 데이터 검증용. 게이트웨이에 노출하지 않는다."""
    try:
        return tax_calc.calculate(req)
    except tax_calc.CalcShortError as e:
        raise HTTPException(422, {"error": e.code, "message": str(e), "ticker": e.ticker, "short_qty": str(e.short_qty)})


@app.post("/v1/inquiries", response_model=InquiryOut)
async def create_inquiry(body: InquiryIn):
    if body.tax_year != TAX_YEAR:
        raise HTTPException(400, {"error": "E-VALIDATION", "message": f"tax_year must be {TAX_YEAR}"})
    trace_id = f"tr_{uuid.uuid4().hex[:12]}"
    t0 = time.perf_counter()

    try:
        judgment = await get_judge().classify(body.text)
    except Exception as e:  # F02-4: 폴백 없음
        return InquiryOut(trace_id=trace_id, status="REVIEW", category="UNKNOWN", confidence=0, error_code="E-JUDGE",
                          latency_ms=int((time.perf_counter() - t0) * 1000))

    gate = judgment.gate()
    if gate in ("REJECTED", "REVIEW"):
        return InquiryOut(trace_id=trace_id, status=gate, category=judgment.category, confidence=judgment.confidence,
                          latency_ms=int((time.perf_counter() - t0) * 1000))

    ctx = registry.ToolContext(body.customer_id, body.tax_year)
    result = await react.run(body.text, judgment, get_client(), ctx)
    # TODO 4일차: F07 조립·숫자 검증. 지금은 에이전트 final을 그대로 draft로.
    status = "FAILED" if result.aborted in ("E-LLM-FORMAT", "E-TOOL-FAIL") else \
             "REVIEW" if result.aborted or gate == "RUN_REVIEW" else "AUTO"
    return InquiryOut(trace_id=trace_id, status=status, category=judgment.category, confidence=judgment.confidence,
                      draft=result.final, steps=len(result.steps), error_code=result.aborted,
                      latency_ms=int((time.perf_counter() - t0) * 1000))
