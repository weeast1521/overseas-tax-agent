"""F01 민원 접수 API (FastAPI).

POST /v1/inquiries 처리 흐름 (명세서 F01):
  요청 검증 → trace 생성 → F02 Judge → 게이팅 → F11 방식 결정 → F05 계산(figures)
  → F03 에이전트 → (F07 조립·검증: SCRUM-35·36) → status 결정

숫자(figures)는 에이전트와 무관하게 tax_calc로 직접 계산한다. LLM이 실패해도 상담원은 숫자를 받는다.
"""
from __future__ import annotations

import os
import time
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from app import repo
from app.agent import react
from app.judge.base import Judgment, get_judge
from app.llm.base import get_client
from app.schemas import ErrorBody, InquiryRequest, InquiryResponse, MethodInfo
from app.tools import registry, tax_calc

TAX_YEAR = int(os.getenv("TAX_YEAR", "2025"))

app = FastAPI(title="overseas-tax-agent", version="0.2.0")


def _judge():  # 테스트에서 교체
    return get_judge()


def _llm():
    return get_client()


def _trace_id() -> str:
    return f"tr_{uuid.uuid4().hex[:12]}"


def _error(status_code: int, code: str, message: str, trace_id: str | None = None):
    return JSONResponse(status_code=status_code, content=ErrorBody(error=code, message=message, trace_id=trace_id).model_dump())


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
    except tax_calc.CalcError as e:
        raise HTTPException(422, {"error": e.code, "message": str(e)})


def compute_figures(customer_id: int, tax_year: int, methods: dict[str, str]) -> tuple[dict | None, str | None]:
    """F05를 직접 돌려 figures를 만든다. 실패하면 (None, 에러코드)."""
    rows = repo.load_trades(customer_id)
    if not rows:
        return None, "E-NO-TRADES"
    try:
        res = tax_calc.calculate(tax_calc.CalcRequest(
            customer_id=customer_id, tax_year=tax_year,
            trades=[tax_calc.Trade(**r) for r in rows], account_methods=methods))
    except tax_calc.CalcError as e:
        return None, e.code
    fig = res.model_dump(mode="json")
    fig["transferred_lot_sales"] = [s.sell_trade_id for s in res.sales if s.has_transferred_lot]
    return fig, None


def decide_status(gate: str, needs_confirmation: list[str], figures: dict | None,
                  agent_aborted: str | None, calc_error: str | None) -> tuple[str, str | None, str | None]:
    """(status, review_reason, error_code). 우선순위: FAILED > METHOD_UNCONFIRMED > TRANSFER_REVIEW > AGENT_ABORTED > LOW_CONFIDENCE > AUTO."""
    if agent_aborted in ("E-LLM-FORMAT", "E-TOOL-FAIL"):
        return "FAILED", None, agent_aborted
    if calc_error in ("E-CALC-FX",):
        return "FAILED", None, calc_error
    if needs_confirmation:
        return "REVIEW", "METHOD_UNCONFIRMED", "E-METHOD-UNCONFIRMED"
    if figures and figures.get("transferred_lot_sales"):
        return "REVIEW", "TRANSFER_REVIEW", None
    if calc_error:  # E-CALC-SHORT, E-CALC-TRANSFER: 거래 누락 가능성 → 사람이 본다
        return "REVIEW", "AGENT_ABORTED", calc_error
    if agent_aborted:  # E-LOOP, E-MAXSTEP
        return "REVIEW", "AGENT_ABORTED", agent_aborted
    if gate == "RUN_REVIEW":
        return "REVIEW", "LOW_CONFIDENCE", None
    return "AUTO", None, None


@app.post("/v1/inquiries", response_model=InquiryResponse, responses={400: {"model": ErrorBody}})
async def create_inquiry(body: InquiryRequest):
    if body.tax_year != TAX_YEAR:
        return _error(400, "E-VALIDATION", f"tax_year must be {TAX_YEAR}")
    if body.mode == "async":
        return _error(400, "E-VALIDATION", "mode=async is not available yet (S2)")

    trace_id = _trace_id()
    t0 = time.perf_counter()
    ms = lambda: int((time.perf_counter() - t0) * 1000)  # noqa: E731

    # F02 Judge — 실패 시 폴백 없음 (F02-4)
    try:
        judgment: Judgment = await _judge().classify(body.text)
    except Exception:
        return InquiryResponse(trace_id=trace_id, status="REVIEW", review_reason="AGENT_ABORTED",
                               category="UNKNOWN", confidence=0, error_code="E-JUDGE", latency_ms=ms())

    gate = judgment.gate()
    if gate == "REJECTED":
        return InquiryResponse(trace_id=trace_id, status="REJECTED", category=judgment.category,
                               confidence=judgment.confidence, latency_ms=ms())
    if gate == "REVIEW":
        return InquiryResponse(trace_id=trace_id, status="REVIEW", review_reason="LOW_CONFIDENCE",
                               category=judgment.category, confidence=judgment.confidence, latency_ms=ms())

    # F11 방식 결정
    m = repo.resolve_methods(body.customer_id, body.method_override)
    method = MethodInfo(applied=body.method_override, brokers=m["brokers"], needs_confirmation=m["needs_confirmation"])

    # F05 figures — 에이전트와 독립
    figures, calc_error = compute_figures(body.customer_id, body.tax_year, m["methods"])

    # F03 에이전트 — 초안 재료. F07 조립·검증은 SCRUM-35·36에서 이 자리에 끼운다
    ctx = registry.ToolContext(body.customer_id, body.tax_year, body.method_override)
    result = await react.run(body.text, judgment, _llm(), ctx)

    status, reason, err = decide_status(gate, m["needs_confirmation"], figures, result.aborted, calc_error)
    return InquiryResponse(
        trace_id=trace_id, status=status, review_reason=reason, category=judgment.category,
        confidence=judgment.confidence, method=method, draft=result.final, figures=figures,
        steps=len(result.steps), latency_ms=ms(), error_code=err,
    )
