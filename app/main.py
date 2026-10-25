"""F01 민원 접수 API (FastAPI).

POST /v1/inquiries 처리 흐름 (명세서 F01):
  요청 검증 → trace 생성 → F02 Judge → 게이팅 → F11 방식 결정 → F05 계산(figures)
  → F03 에이전트 → status 결정 → F07 조립(SCRUM-35; 검증은 SCRUM-36)

숫자(figures)는 에이전트와 무관하게 tax_calc로 직접 계산한다. LLM이 실패해도 상담원은 숫자를 받는다.
"""
from __future__ import annotations

import os
import time
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from app import repo, trace
from app.agent import react
from app.answer import assemble
from app.judge.base import Judgment, get_judge
from app.judge.claude_judge import JudgeError
from app.llm.base import get_client
from app.schemas import ErrorBody, InquiryRequest, InquiryResponse, MethodInfo
from app.tools import registry, tax_calc

TAX_YEAR = int(os.getenv("TAX_YEAR", "2025"))
ASSEMBLER_ON = os.getenv("ANSWER_ASSEMBLER", "on").lower() not in ("off", "0", "false")

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


def _record(body: InquiryRequest, resp: InquiryResponse, result: react.AgentResult | None, judge, llm, gate: str | None) -> None:
    """F03-6: 판단·스텝·결과를 agent_trace에 남긴다. 실패해도 응답은 그대로 나간다."""
    trace.save_safely({
        "trace_id": resp.trace_id, "customer_id": body.customer_id, "tax_year": body.tax_year, "inquiry_text": body.text,
        "category": resp.category, "confidence": resp.confidence, "gate": gate, "status": resp.status,
        "review_reason": resp.review_reason, "error_code": resp.error_code, "aborted": result.aborted if result else None,
        "judge_name": getattr(judge, "name", None), "llm_name": getattr(llm, "name", None), "llm_model": getattr(llm, "model", None),
        "steps": resp.steps, "tokens_in": resp.tokens_in, "tokens_out": resp.tokens_out, "latency_ms": resp.latency_ms,
        "final_text": resp.draft,
    }, trace.steps_from_agent(result))


async def handle_inquiry(body: InquiryRequest) -> tuple[InquiryResponse, react.AgentResult | None]:
    """F01 파이프라인 본체. API 핸들러와 e2e 스크립트가 같이 쓴다. 두 번째 값은 에이전트 trace(없으면 None)."""
    trace_id = _trace_id()
    t0 = time.perf_counter()
    ms = lambda: int((time.perf_counter() - t0) * 1000)  # noqa: E731
    judge = _judge()

    # F02 Judge — 민원 문장만 넘긴다. customer_id·계좌는 넘기지 않는다 (F02-3). 실패 시 폴백 없음 (F02-4)
    try:
        judgment: Judgment = await judge.classify(body.text)
    except Exception as e:  # JudgeError(E-JUDGE) / JudgeFormatError(E-LLM-FORMAT) / 기타
        resp = InquiryResponse(trace_id=trace_id, status="REVIEW", review_reason="AGENT_ABORTED",
                               category="UNKNOWN", confidence=0, error_code=e.code if isinstance(e, JudgeError) else "E-JUDGE",
                               latency_ms=ms())
        _record(body, resp, None, judge, None, None)
        return resp, None

    gate = judgment.gate()
    if gate in ("REJECTED", "REVIEW"):
        resp = InquiryResponse(trace_id=trace_id, status=gate, review_reason="LOW_CONFIDENCE" if gate == "REVIEW" else None,
                               category=judgment.category, confidence=judgment.confidence, latency_ms=ms())
        _record(body, resp, None, judge, None, gate)
        return resp, None

    # F11 방식 결정
    m = repo.resolve_methods(body.customer_id, body.method_override)
    method = MethodInfo(applied=body.method_override, brokers=m["brokers"], needs_confirmation=m["needs_confirmation"])

    # F05 figures — 에이전트와 독립
    figures, calc_error = compute_figures(body.customer_id, body.tax_year, m["methods"])

    # F03 에이전트 — Judge 결과(유형·필요 툴·확신도)는 react.run이 시스템 프롬프트에 주입한다 (F03-6)
    llm = _llm()
    ctx = registry.ToolContext(body.customer_id, body.tax_year, body.method_override)
    result = await react.run(body.text, judgment, llm, ctx)

    status, reason, err = decide_status(gate, m["needs_confirmation"], figures, result.aborted, calc_error)

    # F07 조립 — 고객용 문장은 코드가 짠 골격 + 숫자·상태·근거 줄, LLM은 설명만. ANSWER_ASSEMBLER=off면 에이전트 원문(대조 실험용)
    draft_text, note, sources, a_in, a_out = result.final, None, [], 0, 0
    if ASSEMBLER_ON and status != "FAILED":
        d = await assemble.assemble(judgment.category, body.text, figures, ctx.chunks, m["needs_confirmation"], llm=llm,
                                    agent_final=result.final, calc_error=calc_error, aborted=result.aborted, tax_year=body.tax_year)
        draft_text, note, sources, a_in, a_out = d.customer_text, d.agent_note, d.sources, d.tokens_in, d.tokens_out
    resp = InquiryResponse(
        trace_id=trace_id, status=status, review_reason=reason, category=judgment.category,
        confidence=judgment.confidence, method=method, draft=draft_text, agent_note=note, figures=figures,
        sources=sources, steps=len(result.steps), latency_ms=ms(),
        tokens_in=sum(st.in_tokens for st in result.steps) + a_in, tokens_out=sum(st.out_tokens for st in result.steps) + a_out,
        error_code=err,
    )
    _record(body, resp, result, judge, llm, gate)
    return resp, result


@app.get("/v1/traces/{trace_id}")
async def get_trace(trace_id: str):
    """F09 "근거 보기". 스텝별 thought·tool·args·observation. 게이트웨이가 인증 후 프록시한다."""
    t = trace.get_store().get(trace_id)
    if t is None:
        return _error(404, "E-NOT-FOUND", "no such trace", trace_id)
    return t


@app.get("/v1/traces")
async def list_traces(status: str | None = None, limit: int = 50):
    """검토 대기 목록 등. ?status=REVIEW"""
    return {"items": trace.get_store().list(status=status, limit=min(limit, 200))}


@app.post("/v1/inquiries", response_model=InquiryResponse, responses={400: {"model": ErrorBody}})
async def create_inquiry(body: InquiryRequest):
    if body.tax_year != TAX_YEAR:
        return _error(400, "E-VALIDATION", f"tax_year must be {TAX_YEAR}")
    if body.mode == "async":
        return _error(400, "E-VALIDATION", "mode=async is not available yet (S2)")
    resp, _ = await handle_inquiry(body)
    return resp
