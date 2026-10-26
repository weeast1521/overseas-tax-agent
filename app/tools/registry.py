"""툴 레지스트리. 에이전트는 이름으로만 툴을 부른다.

지금 들어 있는 것: tax_calc (완성), sql_query·reg_search (3일차에 채울 스텁).
F03-4: tax_calc는 sql_query 결과가 history에 있어야 호출 가능 → E-TOOL-ORDER.
"""
from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from app.llm.base import ToolSpec
from app.tools import tax_calc

ToolFn = Callable[[dict[str, Any], "ToolContext"], Awaitable[dict[str, Any]]]


class ToolContext:
    def __init__(self, customer_id: int, tax_year: int, method_override: str | None = None):
        self.customer_id = customer_id
        self.tax_year = tax_year
        self.method_override = method_override
        self.last_rows: list[dict] | None = None  # sql_query 결과 (tax_calc 입력)
        self.chunks: list[dict] = []  # reg_search가 돌려준 조문 누적 — 조립기(F07)의 인용 허용 집합. LLM 출력이 아니라 여기가 출처의 진실


SPECS: dict[str, ToolSpec] = {
    "sql_query": ToolSpec(
        name="sql_query",
        description="고객의 해외주식 체결·결제일·결제일 환율·수수료를 조회한다. 전 계좌(증권사 무관)에서 가져온다.",
        input_schema={"type": "object", "properties": {"intent": {"type": "string", "description": "조회 의도 (자연어)"},
                                                       "tickers": {"type": "array", "items": {"type": "string"}}},
                      "required": ["intent"]},
    ),
    "tax_calc": ToolSpec(
        name="tax_calc",
        description="sql_query로 조회한 거래로 계좌별 lot 매칭(증권사 방식대로 FIFO/AVG)·이체 lot 원가 복원·결제일 환율·납세자 단위 통산·기본공제·세액을 계산한다. 숫자는 이 툴 출력만 쓴다.",
        input_schema={"type": "object", "properties": {"method_override": {"type": "string", "enum": ["FIFO", "AVG"], "description": "상담원이 특정 계좌 방식을 확정했을 때만"}}},
    ),
    "broker_method": ToolSpec(
        name="broker_method",
        description="고객 계좌별 증권사 산정 방식(FIFO/AVG/CHOICE)과 확인이 필요한 계좌를 알려준다. 어느 방식이 유리한지는 판단하지 않는다.",
        input_schema={"type": "object", "properties": {}},
    ),
    "reg_search": ToolSpec(
        name="reg_search",
        description="세법·시행령·국세청 안내·증권사 FAQ에서 규정 근거 조문을 찾는다.",
        input_schema={"type": "object", "properties": {"query": {"type": "string"}, "k": {"type": "integer", "default": 5}},
                      "required": ["query"]},
    ),
}


async def _sql_query(args: dict, ctx: ToolContext) -> dict:
    # 3일차: Text-to-SQL + sqlglot 검증 + DB 실행. 지금은 리포지토리에서 직접 읽는 스텁.
    from app.repo import load_trades
    rows = load_trades(ctx.customer_id, tickers=args.get("tickers"))
    ctx.last_rows = rows
    return {"row_count": len(rows), "rows": rows[:500], "truncated": len(rows) > 500}


async def _tax_calc(args: dict, ctx: ToolContext) -> dict:
    if ctx.last_rows is None:
        return {"error": "E-TOOL-ORDER", "message": "sql_query를 먼저 호출해 거래를 조회하세요"}
    try:
        from app.repo import load_trades
        trades = load_trades(ctx.customer_id)  # 큐 구성을 위해 전 종목 전 기간
        from app.repo import account_methods
        req = tax_calc.CalcRequest(customer_id=ctx.customer_id, tax_year=ctx.tax_year,
                                   trades=[tax_calc.Trade(**r) for r in trades],
                                   account_methods=account_methods(ctx.customer_id, args.get("method_override") or ctx.method_override, ctx.tax_year))
        res = tax_calc.calculate(req)
        # 요약을 앞에: Observation은 2,000자에서 잘리므로 에이전트가 반드시 봐야 할 숫자를 먼저 둔다
        return {
            "summary": {"tax_year": res.tax_year, "total_gain_krw": res.total_gain_krw, "deduction_krw": res.deduction_krw,
                        "taxable_krw": res.taxable_krw, "tax_krw": res.tax_krw,
                        "by_account": [{"account_id": a.account_id, "broker_code": a.broker_code, "method": a.method, "gain_krw": a.gain_krw} for a in res.by_account],
                        "transferred_lot_sales": [s.sell_trade_id for s in res.sales if s.has_transferred_lot]},
            "detail": json.loads(res.model_dump_json()),
        }
    except tax_calc.CalcShortError as e:
        return {"error": e.code, "message": str(e), "account_id": e.account_id, "ticker": e.ticker,
                "short_qty": str(e.short_qty), "hint": "이 계좌에 lot이 부족. 다른 계좌에서 이체(대체입고)된 주식이면 TRANSFER 기록이 누락된 것"}
    except tax_calc.CalcError as e:
        return {"error": e.code, "message": str(e)}


async def _broker_method(args: dict, ctx: ToolContext) -> dict:
    """F11 (SCRUM-47). 증권사 기본 방식 + 고객 저장값(customer_method) + override. 어느 방식이 유리한지는 내지 않는다 (F11-3)."""
    from app.repo import resolve_methods
    m = resolve_methods(ctx.customer_id, ctx.method_override, ctx.tax_year)
    return {"accounts": m["brokers"], "applied": m["methods"], "needs_confirmation": m["needs_confirmation"],
            "confirmed": {k: {"method": v["method"], "changed_at": v["changed_at"]} for k, v in m["confirmed"].items()},
            "as_of": "2025-04", "note": "선택제 계좌는 고객 확인(저장) 전까지 FIFO로 계산"}


async def _reg_search(args: dict, ctx: ToolContext) -> dict:
    # 2~3일차: pgvector 하이브리드 검색. 지금은 data/corpus/*.json 키워드 매칭 스텁.
    from app.repo import search_corpus
    chunks = search_corpus(args["query"], k=int(args.get("k", 5)))
    ctx.chunks.extend(chunks)
    return {"chunks": chunks, "rewritten_query": None}


TOOLS: dict[str, ToolFn] = {"sql_query": _sql_query, "broker_method": _broker_method, "tax_calc": _tax_calc, "reg_search": _reg_search}


def specs(names: list[str] | None = None) -> list[ToolSpec]:
    return [SPECS[n] for n in (names or SPECS)]


async def invoke(name: str, args: dict, ctx: ToolContext) -> dict:
    """툴 호출. 모델이 인자를 빠뜨리거나 툴이 예외를 던져도 루프는 죽지 않는다 — Observation에 에러 코드로 돌려준다."""
    if name not in TOOLS:
        return {"error": "E-TOOL-UNKNOWN", "message": f"no tool {name}"}
    args = args if isinstance(args, dict) else {}
    missing = [k for k in SPECS[name].input_schema.get("required", []) if k not in args]
    if missing:
        return {"error": "E-TOOL-ARGS", "message": f"{name}: 필수 인자 누락 {missing}", "required": SPECS[name].input_schema.get("required", [])}
    try:
        return await TOOLS[name](args, ctx)
    except Exception as e:  # 툴 내부 예외 → 루프가 아니라 Observation으로 (평가 1차에서 Haiku가 reg_search를 인자 없이 불러 KeyError로 크래시)
        return {"error": "E-TOOL-EXC", "message": f"{name}: {type(e).__name__}: {str(e)[:200]}"}
