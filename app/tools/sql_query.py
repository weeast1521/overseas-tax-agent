"""F04-1 Text-to-SQL 생성 (SCRUM-26) + F04-7 거부 시 1회 재생성 (SCRUM-27).

흐름: intent(자연어) → LLM이 SQL 제안 → sql_guard(검사·고객 바인드) → sql_exec(읽기 전용) → rows
LLM이 없거나(stub) 실패하면 TEMPLATES 의 결정론 SQL로 폴백 — 평가 구성 stub 과 테스트가 이 경로를 쓴다.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from app.llm.base import LLMClient, Message
from app.tools import sql_exec, sql_guard

SCHEMA_DOC = """거래 DB 스키마 (읽기 전용, 고객 1명 범위):
- trade(trade_id, customer_id, account_id, broker_code, ticker, side, qty, price_usd, fee_usd, trade_date, settle_date, fx_settle, transfer_id)
  side: BUY | SELL | TRANSFER_IN | TRANSFER_OUT. settle_date = 결제일(세법상 양도·취득일, 환율 기준일). fx_settle = 결제일 매매기준율(원/달러).
  TRANSFER_* 는 다른 증권사로 옮긴 행(price_usd 0, transfer_id 로 OUT/IN 짝). qty·금액은 문자열 숫자.
- account(account_id, customer_id, broker_code)   예: MIRAE-2001 / NAMUH-2001
- customer(customer_id)
- fx_rate(rate_date, usd_krw)                      결제일 환율 조인: fx_rate.rate_date = trade.settle_date
- settlement_daily(customer_id, settle_date, account_id, buy_krw, sell_krw, fee_krw)   일배치 집계 (비어 있을 수 있음)

규칙:
1. SELECT 한 문장만. INSERT/UPDATE/DELETE/DDL 금지. 위 다섯 테이블만.
2. 고객 필터는 반드시 `customer_id = :customer_id` (플레이스홀더). 숫자 고객 ID를 직접 쓰지 않는다. 시스템이 세션 고객을 바인드한다.
3. 과세연도 필터는 settle_date 기준 (예: settle_date BETWEEN '2025-01-01' AND '2025-12-31').
4. 종목은 ticker = 'TSLA' 처럼 대문자. 결과는 500행 이내.
5. **단순하게.** 이 조회 결과는 그대로 tax_calc 에 들어가 lot 매칭·환율·손익 계산이 거기서 된다. 그러니 trade 행을 가져오는 SELECT 한 줄로 끝낸다:
   `SELECT *` 또는 `SELECT t.*, f.usd_krw`. UNION / CASE / 집계·GROUP BY / 서브쿼리 / 구간 라벨 열은 쓰지 않는다 (집계는 의도가 명시적으로 집계일 때만).
   이체(TRANSFER) 행, 다른 연도의 매수 행도 tax_calc 가 필요로 하므로 매도만 고르거나 연도로 자르지 말고, 의도가 특정 종목이면 ticker 만 좁힌다.
   300자 이내.
6. SQL만 출력한다. 설명·코드블록 없이."""

FEWSHOT: list[tuple[str, str]] = [
    ("고객의 전체 거래 내역",
     "SELECT * FROM trade WHERE customer_id = :customer_id ORDER BY settle_date, trade_id"),
    ("테슬라(TSLA) 거래만",
     "SELECT * FROM trade WHERE customer_id = :customer_id AND ticker = 'TSLA' ORDER BY settle_date"),
    ("2025년 결제분 거래",
     "SELECT * FROM trade WHERE customer_id = :customer_id AND settle_date BETWEEN '2025-01-01' AND '2025-12-31' ORDER BY settle_date"),
    ("고객의 2025년 테슬라 매도와 결제일 환율",
     "SELECT t.*, f.usd_krw FROM trade t JOIN fx_rate f ON f.rate_date = t.settle_date "
     "WHERE t.customer_id = :customer_id AND t.ticker = 'TSLA' AND t.side = 'SELL' AND t.settle_date BETWEEN '2025-01-01' AND '2025-12-31' ORDER BY t.settle_date"),
    ("2025년 엔비디아 매도 체결과 결제일 환율, 이체 입고된 lot 의 원 취득 내역, 수수료 (전 계좌)",  # 세금 계산용 — 매도만 고르지 않는다
     "SELECT t.*, f.usd_krw FROM trade t LEFT JOIN fx_rate f ON f.rate_date = t.settle_date "
     "WHERE t.customer_id = :customer_id AND t.ticker = 'NVDA' ORDER BY t.settle_date, t.trade_id"),
    ("2025년 전 계좌 해외주식 매수·매도 체결, 결제일, 결제일 환율, 수수료, 계좌 간 이체 내역",
     "SELECT * FROM trade WHERE customer_id = :customer_id ORDER BY settle_date, trade_id"),
    ("계좌별 매수·매도 건수와 수량 합계",
     "SELECT account_id, broker_code, side, COUNT(*) AS n, SUM(qty) AS qty_sum FROM trade WHERE customer_id = :customer_id GROUP BY account_id, broker_code, side"),
    ("다른 증권사로 옮긴(이체) 거래 행만",
     "SELECT * FROM trade WHERE customer_id = :customer_id AND side IN ('TRANSFER_OUT', 'TRANSFER_IN') ORDER BY transfer_id, side"),
    ("연도별 매도 건수",
     "SELECT substr(settle_date, 1, 4) AS yr, COUNT(*) AS sells FROM trade WHERE customer_id = :customer_id AND side = 'SELL' GROUP BY substr(settle_date, 1, 4)"),
    ("NH투자증권(나무) 계좌 거래만",
     "SELECT * FROM trade WHERE customer_id = :customer_id AND broker_code = 'NAMUH' ORDER BY settle_date"),
]

# LLM 없이도 도는 결정론 템플릿 (stub 구성·폴백). intent 키워드로 고른다.
_SHOT = dict(FEWSHOT)
TEMPLATES: list[tuple[str, str]] = [
    (r"환율|FX|달러", "SELECT t.*, f.usd_krw FROM trade t LEFT JOIN fx_rate f ON f.rate_date = t.settle_date WHERE t.customer_id = :customer_id ORDER BY t.settle_date"),
    (r"계좌별|증권사별|합계|집계", _SHOT["계좌별 매수·매도 건수와 수량 합계"]),
    (r".*", _SHOT["고객의 전체 거래 내역"]),
]
# 이체 행만 고르는 템플릿은 두지 않는다 — 세금 계산(tax_calc)은 전 거래가 필요하고, 에이전트 의도에는 거의 항상 '이체'가 들어간다



def _log(rec: dict) -> None:
    """SQL_LOG(JSONL) 에 생성 SQL 전문을 남긴다. trace 의 observation 은 2,000자에서 잘려 긴 SQL 을 못 본다 (scripts/show_sql.py)."""
    path = os.getenv("SQL_LOG", "")
    if not path:
        return
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass


class SqlGenError(Exception):
    code = "E-SQL-GEN"


def template_sql(intent: str, tickers: list[str] | None, tax_year: int | None) -> str:
    sql = next(s for pat, s in TEMPLATES if re.search(pat, intent or "", re.I))
    extra = []
    if tickers:
        extra.append("ticker IN (" + ", ".join(f"'{t.upper()}'" for t in tickers if re.fullmatch(r"[A-Za-z0-9.\-]{1,10}", t)) + ")")
    if extra:
        # WHERE … 뒤, ORDER BY/GROUP BY 앞에 끼운다
        m = re.search(r"\s(ORDER BY|GROUP BY)\s", sql)
        cond = " AND " + " AND ".join(extra)
        sql = (sql[: m.start()] + cond + sql[m.start():]) if m else sql + cond
    return sql


def _strip(text: str) -> str:
    text = re.sub(r"```(?:sql)?", "", text or "", flags=re.I).strip()
    m = re.search(r"(SELECT|WITH)\b.*", text, re.I | re.S)
    return (m.group(0) if m else text).strip().rstrip(";")


async def generate_sql(llm: LLMClient | None, intent: str, tickers: list[str] | None, tax_year: int | None,
                       reject_reason: str | None = None) -> tuple[str, str]:
    """(sql, how) — how: llm | llm-retry | template. LLM이 없거나 빈 답이면 템플릿."""
    if llm is None or getattr(llm, "name", "") not in ("claude", "ollama"):  # 실제 LLM 제공자만. 대본·가짜 LLM은 템플릿
        return template_sql(intent, tickers, tax_year), "template"
    shots = "\n".join(f"의도: {i}\nSQL: {s}" for i, s in FEWSHOT)
    hint = f"\n(과세연도 {tax_year})" if tax_year else ""
    hint += f"\n(종목: {', '.join(tickers)})" if tickers else ""
    msgs = [Message(role="system", content=SCHEMA_DOC + "\n\n예시:\n" + shots),
            Message(role="user", content=f"의도: {intent}{hint}" + (f"\n\n이전 SQL은 거부됨 — 사유: {reject_reason}. 규칙을 지켜 다시 작성." if reject_reason else "") + "\nSQL:")]
    try:
        rep = await llm.complete(msgs, [])
    except Exception as e:
        raise SqlGenError(f"LLM 호출 실패: {type(e).__name__}") from e
    sql = _strip(rep.text or "")
    if not sql:
        raise SqlGenError("빈 응답")
    return sql, ("llm-retry" if reject_reason else "llm")


async def run_sql_query(llm: LLMClient | None, intent: str, tickers: list[str] | None, customer_id: int, tax_year: int | None) -> dict:
    """툴 본체. 결과 dict 는 에이전트 Observation 이자 trace 기록."""
    d = sql_exec.dialect()
    attempts: list[dict] = []
    reason = None
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "intent": intent, "tickers": tickers, "model": getattr(llm, "model", getattr(llm, "name", None)), "attempts": attempts}
    for attempt in range(2):  # 거부 시 1회 재생성 (F04-7)
        t0 = time.perf_counter()
        try:
            sql, how = await generate_sql(llm, intent, tickers, tax_year, reject_reason=reason)
        except SqlGenError as e:
            attempts.append({"how": "gen", "error": str(e)})
            sql, how = template_sql(intent, tickers, tax_year), "template"  # 생성 실패는 템플릿으로 — 숫자 조회는 멈추면 안 된다
        gen_ms = int((time.perf_counter() - t0) * 1000)
        try:
            g = sql_guard.bind_customer(sql, d)
        except sql_guard.SqlScopeError as e:
            attempts.append({"how": how, "gen_ms": gen_ms, "sql": sql, "rejected": e.reason, "detail": e.detail})
            reason = f"{e.reason} {e.detail}".strip()
            if how == "template":
                break
            continue
        try:
            res = sql_exec.get_executor().execute(g.sql, customer_id)
        except sql_exec.SqlExecError as e:
            attempts.append({"how": how, "gen_ms": gen_ms, "sql": g.sql, "error": str(e)})
            _log({**rec, "result": "E-SQL-EXEC", "message": str(e)})
            return {"error": "E-SQL-EXEC", "message": str(e), "sql": g.sql, "attempts": _short(attempts)}
        _log({**rec, "result": how, "gen_ms": gen_ms, "sql": g.sql, "row_count": res["row_count"], "elapsed_ms": res["elapsed_ms"]})
        # 요약을 앞에: Observation 은 2,000자에서 잘리므로 rows 는 뒤에 둔다 (tax_calc 는 ctx.last_rows 로 전체를 받는다)
        return {"sql": g.sql, "generated_by": how, "row_count": res["row_count"], "truncated": res["truncated"], "elapsed_ms": res["elapsed_ms"],
                "customer_filter_injected": g.customer_filter_injected, "limit_added": g.limit_added, "attempts": _short(attempts),
                "columns": res["columns"], "rows": res["rows"]}
    _log({**rec, "result": "E-SQL-GEN", "message": reason})
    return {"error": "E-SQL-GEN", "message": f"SQL 생성 2회 거부: {reason}", "attempts": _short(attempts)}


def _short(attempts: list[dict]) -> list[dict]:
    return [{**a, "sql": a["sql"][:300]} if "sql" in a else a for a in attempts]
