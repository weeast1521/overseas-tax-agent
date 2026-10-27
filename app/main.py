"""F01 민원 접수 API (FastAPI).

POST /v1/inquiries 처리 흐름 (명세서 F01):
  요청 검증 → trace 생성 → F02 Judge → 게이팅 → F11 방식 결정 → F05 계산(figures)
  → F03 에이전트 → status 결정 → F07 조립(SCRUM-35; 검증은 SCRUM-36)

숫자(figures)는 에이전트와 무관하게 tax_calc로 직접 계산한다. LLM이 실패해도 상담원은 숫자를 받는다.
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid

from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse

from app import method_store, repo, trace
from app.agent import react
from app.answer import assemble, verify
from app.judge.base import Judgment, get_judge
from app.judge.claude_judge import JudgeError
from app.llm.base import get_client
from app.schemas import BrokerMethodRequest, ErrorBody, InquiryRequest, InquiryResponse, MethodInfo, MethodSetRequest
from app.tools import registry, tax_calc

TAX_YEAR = int(os.getenv("TAX_YEAR", "2025"))
ASSEMBLER_ON = os.getenv("ANSWER_ASSEMBLER", "on").lower() not in ("off", "0", "false")

app = FastAPI(title="overseas-tax-agent", version="0.2.0")


def _judge():  # 테스트에서 교체
    return get_judge()


def _llm():
    return get_client()


def _assembler_llm(agent_llm):
    """조립기 LLM. 설명 2문장·200자라 에이전트만큼 큰 모델이 필요 없다."""
    # 기본 claude-sonnet-5-5 (10/26 홀드아웃: Opus 조립과 문장 점수 차이 없음, E-TIMEOUT 11→2에 기여). 'same'이면 에이전트와 같은 LLM
    name = os.getenv("ASSEMBLER_MODEL", "claude-sonnet-5-5")
    if name == "same" or getattr(agent_llm, "name", "") != "claude" or name == getattr(agent_llm, "model", None):
        return agent_llm
    from app.llm.claude_client import ClaudeClient
    return ClaudeClient(model=name)


def _sub_llm(agent_llm, env: str, default: str):
    """보조 LLM (SQL 생성 등). 'same'이면 에이전트와 같은 LLM. Claude 가 아닌 제공자(ollama·stub)는 그대로."""
    name = os.getenv(env, default)
    if name == "same" or getattr(agent_llm, "name", "") != "claude" or name == getattr(agent_llm, "model", None):
        return agent_llm
    from app.llm.claude_client import ClaudeClient
    return ClaudeClient(model=name)


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


@app.post("/v1/tools/broker_method")
async def tool_broker_method(req: BrokerMethodRequest):
    """디버그용 (F11). 게이트웨이에 노출하지 않는다."""
    ctx = registry.ToolContext(req.customer_id, req.tax_year, req.method_override)
    return await registry.invoke("broker_method", {}, ctx)


@app.post("/v1/admin/corpus/reindex")
async def admin_reindex(x_admin_token: str | None = Header(default=None)):
    """F06-4 재색인 (SCRUM-32). 관리자 토큰(ADMIN_TOKEN) 필요. 새 버전으로 쓰고 이전 버전은 남긴다. 게이트웨이에 노출하지 않는다."""
    if not os.getenv("ADMIN_TOKEN") or x_admin_token != os.getenv("ADMIN_TOKEN"):
        return _error(403, "E-FORBIDDEN", "admin token required")
    import asyncio
    import importlib.util
    spec = importlib.util.spec_from_file_location("index_corpus", Path(__file__).resolve().parents[1] / "scripts" / "index_corpus.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    result = await asyncio.to_thread(mod.run)
    from app.rag import search
    search.reset_searcher()
    return result


@app.post("/v1/customers/{customer_id}/accounts/{account_id}/method", responses={409: {"model": ErrorBody}, 422: {"model": ErrorBody}})
async def set_customer_method(customer_id: int, account_id: str, req: MethodSetRequest):
    """F11-2 상담원이 고객에게 확인한 산정 방식 저장 (SCRUM-47). 선택제 계좌만. 계좌·연도당 1회 규칙 → 409 E-METHOD-CHANGE."""
    m = repo.resolve_methods(customer_id, None, req.tax_year)
    if account_id not in m["brokers"]:
        return _error(404, "E-NOT-FOUND", f"{account_id}: 이 고객의 계좌가 아님")
    if m["brokers"][account_id] != "CHOICE":
        return _error(422, "E-METHOD-NOT-CHOICE", f"{account_id}: 증권사 고정 방식({m['brokers'][account_id]}) 계좌라 저장할 수 없음")
    try:
        return method_store.get_store().set(customer_id, account_id, req.tax_year, req.method, req.confirmed_by)
    except method_store.MethodChangeError as e:
        return _error(409, e.code, str(e))


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
    if agent_aborted in ("E-LLM-FORMAT", "E-TOOL-FAIL", "E-TIMEOUT"):
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
    m = repo.resolve_methods(body.customer_id, body.method_override, body.tax_year)
    method = MethodInfo(applied=body.method_override, brokers=m["brokers"], needs_confirmation=m["needs_confirmation"],
                        confirmed={k: v["method"] for k, v in m["confirmed"].items()})

    # F05 figures — 에이전트와 독립
    figures, calc_error = compute_figures(body.customer_id, body.tax_year, m["methods"])

    # F03 에이전트 — Judge 결과(유형·필요 툴·확신도)는 react.run이 시스템 프롬프트에 주입한다 (F03-6)
    llm = _llm()
    ctx = registry.ToolContext(body.customer_id, body.tax_year, body.method_override)
    ctx.category = judgment.category
    # sql_query 의 SQL 생성용. 단순 SELECT 한 줄이라 Haiku 로 충분 — Opus 로 하면 호출당 10초 이상 걸려 E-TIMEOUT 이 19/30 (10/27 t2sql2)
    ctx.llm = _sub_llm(llm, "SQL_MODEL", "claude-haiku-5-5")
    # F01-6 동기 타임아웃 (SCRUM-19). 마감은 접수 시각 기준 — Judge 시간도 포함한다.
    # 취소 안전성: 에이전트 경로는 읽기 전용(sql_query·tax_calc·reg_search)이고 trace는 응답 직전에 한 번 쓰므로 열린 트랜잭션이 없다.
    deadline = t0 + float(os.getenv("SYNC_TIMEOUT_SEC", "30"))
    left = lambda: max(0.05, deadline - time.perf_counter())  # noqa: E731
    result = react.AgentResult()  # 타임아웃으로 취소돼도 여기에 쌓인 스텝은 남는다
    try:
        await asyncio.wait_for(react.run(body.text, judgment, llm, ctx, res=result), timeout=left())
    except asyncio.TimeoutError:
        result.aborted = "E-TIMEOUT"

    status, reason, err = decide_status(gate, m["needs_confirmation"], figures, result.aborted, calc_error)

    # F07 조립 — 고객용 문장은 코드가 짠 골격 + 숫자·상태·근거 줄, LLM은 설명만. ANSWER_ASSEMBLER=off면 에이전트 원문(대조 실험용)
    draft_text, note, sources, a_in, a_out, verify_failures = result.final, None, [], 0, 0, []
    if ASSEMBLER_ON:
        # FAILED여도 숫자·상태 문장은 코드로 조립해 넘긴다 (LLM 설명만 빠짐). 남은 시간 안에 조립 LLM이 못 끝내면 코드 문장만.
        asm_args = dict(agent_final=result.final, calc_error=calc_error, aborted=result.aborted, tax_year=body.tax_year,
                        method_info={**m, "override": body.method_override})
        code_only = lambda: assemble.assemble(judgment.category, body.text, figures, ctx.chunks, m["needs_confirmation"], llm=None, **asm_args)  # noqa: E731
        if status == "FAILED":
            d = await code_only()
        else:
            try:
                d = await asyncio.wait_for(assemble.assemble(judgment.category, body.text, figures, ctx.chunks, m["needs_confirmation"],
                                                             llm=_assembler_llm(llm), **asm_args), timeout=left())
            except asyncio.TimeoutError:
                status, reason, err = "FAILED", None, "E-TIMEOUT"
                d = await code_only()
        draft_text, note, sources, a_in, a_out = d.customer_text, d.agent_note, d.sources, d.tokens_in, d.tokens_out
        # F07-5~8 사후 검증 — 숫자·출처·표현·길이. 하나라도 걸리면 AUTO로 못 나간다 (SCRUM-36)
        v = verify.verify(draft_text, figures, sources, d.facts.get("status_sentences"), raw_llm=d.llm_text)
        if not v.ok:
            verify_failures = v.failures
            note = (note + "\n" if note else "") + "검증 실패: " + "; ".join(f"{f['code']} {f['detail']}" for f in v.failures[:6])
            if status == "AUTO":
                status, reason = "REVIEW", "VERIFY_FAILED"
            err = err or v.codes[0]
        if err == "E-TIMEOUT":
            n = len(result.steps)
            last = next((st.tool for st in reversed(result.steps) if st.tool), None)
            note = f"응답 시간 초과(E-TIMEOUT) — 스텝 {n}까지 진행(마지막 툴 {last or '없음'}). 숫자·상태 문장만 코드로 조립, 규정 설명은 직접 확인\n" + (note or "")
    resp = InquiryResponse(
        trace_id=trace_id, status=status, review_reason=reason, category=judgment.category,
        confidence=judgment.confidence, method=method, draft=draft_text, agent_note=note, figures=figures,
        sources=sources, verify_failures=verify_failures, steps=len(result.steps), latency_ms=ms(),
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
