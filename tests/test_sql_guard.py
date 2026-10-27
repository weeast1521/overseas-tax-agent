"""F04 Text-to-SQL 보안 (SCRUM-26·27). 외부 호출 없음.

수용 조건: 악의 5종 전부 거부(사유 코드가 다름) · customer_id=1042를 쓴 SQL을 세션 1023으로 실행하면 1023만 ·
읽기 전용 → INSERT 는 DB 가 거부(파서 우회 대비) · 느린 쿼리 → E-SQL-EXEC · few-shot 8개 전부 파싱·허용 테이블만·리터럴 고객 없음
"""
import asyncio
import sqlite3

import pytest

from app import repo
from app.llm.base import Completion
from app.tools import sql_exec, sql_guard, sql_query

SYN = repo.DATA / "synthetic" / "trades.json"


@pytest.fixture(autouse=True)
def _synthetic(monkeypatch):
    monkeypatch.setenv("TRADES_FILE", str(SYN))
    sql_exec.reset_executor()


# ---------- SCRUM-27: 검사 ----------

@pytest.mark.parametrize("sql,reason", [
    ("DROP TABLE trade", "NOT_SELECT"),
    ("UPDATE trade SET qty = 0 WHERE customer_id = :customer_id", "NOT_SELECT"),
    ("SELECT * FROM trade WHERE customer_id = 9999", "CUSTOMER_LITERAL"),
    ("SELECT * FROM users", "TABLE_NOT_ALLOWED"),
    ("SELECT * FROM trade; DELETE FROM trade", "MULTI_STATEMENT"),
    ("SELECT * FROM trade WHERE account_id IN (INSERT INTO trade VALUES (1))", "PARSE"),
    ("SELECT pg_sleep(10)", "FORBIDDEN_FUNC"),
    ("SELECT 1", "NO_CUSTOMER_TABLE"),
])
def test_malicious_rejected_with_distinct_reasons(sql, reason):
    with pytest.raises(sql_guard.SqlScopeError) as e:
        sql_guard.bind_customer(sql, "sqlite")
    assert e.value.reason == reason and e.value.code == "E-SQL-SCOPE"


def test_customer_filter_wraps_every_customer_table_and_caps_limit():
    g = sql_guard.bind_customer("SELECT t.ticker, a.broker_code FROM trade t JOIN account a ON a.account_id = t.account_id LIMIT 9999", "sqlite")
    assert g.sql.count("customer_id = :customer_id") == 2 and g.customer_filter_injected  # trade·account 둘 다 감쌈
    assert "LIMIT 500" in g.sql and g.limit_added
    g2 = sql_guard.bind_customer("SELECT * FROM fx_rate WHERE rate_date = '2025-01-02'", "sqlite")
    assert not g2.customer_filter_injected and "LIMIT 500" in g2.sql  # 환율 표는 고객 열이 없음


def test_fewshots_all_valid():
    for intent, sql in sql_query.FEWSHOT:
        g = sql_guard.bind_customer(sql, "sqlite")
        assert g.tables <= sql_guard.ALLOWED_TABLES and ":customer_id" in g.sql, intent
        assert not any(ch.isdigit() for ch in sql.split("customer_id =")[1][:6]), intent  # 리터럴 고객 없음
    assert len(sql_query.FEWSHOT) >= 8


# ---------- SCRUM-27: 실행 (SQLite 읽기 전용) ----------

def test_session_customer_wins_over_sql_literal_attempt():
    """LLM이 1042를 쓰려 해도(리터럴은 거부) 플레이스홀더만 통과하고, 세션 1023 데이터만 나온다."""
    ex = sql_exec.SqliteExecutor()
    g = sql_guard.bind_customer("SELECT DISTINCT customer_id FROM trade", "sqlite")
    res = ex.execute(g.sql, 2001)
    assert [r["customer_id"] for r in res["rows"]] == [2001]
    res2 = ex.execute(g.sql, 2002)
    assert [r["customer_id"] for r in res2["rows"]] == [2002]


def test_db_refuses_write_even_if_parser_bypassed():
    ex = sql_exec.SqliteExecutor()
    with pytest.raises(sql_exec.SqlExecError, match="readonly|read-only|attempt to write"):
        ex.execute("INSERT INTO trade (trade_id, customer_id, account_id, broker_code, ticker, side, qty, trade_date, settle_date) "
                   "VALUES ('x', 1, 'a', 'b', 'T', 'BUY', '1', '2025-01-01', '2025-01-01')", 2001)
    assert ex.execute("SELECT COUNT(*) AS n FROM trade WHERE trade_id = 'x'", 2001)["rows"][0]["n"] == 0


def test_readonly_file_mode(tmp_path, monkeypatch):
    db = tmp_path / "trade.db"
    n = sql_exec.build_from_json(SYN, db)
    assert n == 1983
    monkeypatch.setenv("TRADE_DB", str(db))
    ex = sql_exec.SqliteExecutor()
    with pytest.raises(sql_exec.SqlExecError):
        ex.execute("DELETE FROM trade", 2001)
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT COUNT(*) FROM trade").fetchone()[0] == 1983


def test_slow_query_times_out(monkeypatch):
    monkeypatch.setenv("SQL_TIMEOUT_SEC", "0.3")
    ex = sql_exec.SqliteExecutor()
    heavy = "SELECT COUNT(*) FROM trade a, trade b, trade c"  # 1983^3 행 교차곱 — 검사기를 거치지 않고 실행기만 시험
    with pytest.raises(sql_exec.SqlExecError, match="시간 초과"):
        ex.execute(heavy, 2001)


def test_row_cap_and_truncated_flag():
    ex = sql_exec.SqliteExecutor()
    res = ex.execute("SELECT * FROM trade LIMIT 600", 2001)  # 전 고객 1983행 → 500에서 자름
    assert res["row_count"] == 500 and res["truncated"] is True
    res2 = ex.execute("SELECT * FROM trade WHERE customer_id = :customer_id", 2001)
    assert res2["truncated"] is False


# ---------- SCRUM-26·27: 생성 → 검사 → 실행 ----------

class ClaudeLike:
    """실제 LLM처럼 보이게(name='claude') 대본을 내는 가짜. 첫 답은 거부될 SQL, 재생성은 통과."""
    name = "claude"

    def __init__(self, answers):
        self.answers, self.prompts = list(answers), []

    async def complete(self, messages, tools):
        self.prompts.append(messages[-1].content)
        return Completion(text=self.answers.pop(0))


@pytest.fixture
def _llm_always(monkeypatch):
    monkeypatch.setenv("SQL_GEN", "always")


def test_standard_intent_uses_template_without_llm():
    assert sql_query.is_standard_intent("2025년 전 계좌 해외주식 매도·매수 체결, 결제일 환율, 수수료, 이체 내역")
    assert not sql_query.is_standard_intent("계좌별 매도 건수 합계")
    llm = ClaudeLike([])  # 호출되면 pop 에서 IndexError
    out = asyncio.run(sql_query.run_sql_query(llm, "2025년 엔비디아 매도와 결제일 환율", ["NVDA"], 2001, 2025))
    assert out["generated_by"] == "template" and llm.prompts == [] and "NVDA" in out["sql"]
    llm2 = ClaudeLike(["SELECT account_id, COUNT(*) AS n FROM trade WHERE customer_id = :customer_id AND side = 'SELL' GROUP BY account_id"])
    out2 = asyncio.run(sql_query.run_sql_query(llm2, "계좌별 매도 건수 합계", None, 2001, 2025))
    assert out2["generated_by"] == "llm" and len(llm2.prompts) == 1


def test_llm_sql_rejected_then_regenerated_once(_llm_always):
    llm = ClaudeLike(["```sql\nSELECT * FROM trade WHERE customer_id = 1042 AND ticker='TSLA'\n```",
                      "SELECT * FROM trade WHERE customer_id = :customer_id AND ticker = 'TSLA'"])
    out = asyncio.run(sql_query.run_sql_query(llm, "테슬라 거래", None, 2001, 2025))
    assert out["generated_by"] == "llm-retry" and out["attempts"][0]["rejected"] == "CUSTOMER_LITERAL"
    assert "CUSTOMER_LITERAL" in llm.prompts[1]  # 거부 사유가 프롬프트에 덧붙음
    assert all(r["customer_id"] == 2001 and r["ticker"] == "TSLA" for r in out["rows"])


def test_llm_rejected_twice_is_E_SQL_GEN(_llm_always):
    llm = ClaudeLike(["DROP TABLE trade", "SELECT * FROM users"])
    out = asyncio.run(sql_query.run_sql_query(llm, "x", None, 2001, 2025))
    assert out["error"] == "E-SQL-GEN" and [a["rejected"] for a in out["attempts"]] == ["NOT_SELECT", "TABLE_NOT_ALLOWED"]


def test_template_path_without_llm_matches_acceptance_shape():
    out = asyncio.run(sql_query.run_sql_query(None, "2025년 테슬라 매도와 결제일 환율", ["TSLA"], 2001, 2025))
    assert out["generated_by"] == "template" and "fx_rate" in out["sql"] and "ticker IN ('TSLA')" in out["sql"]
    assert out["rows"] == [] or all(r["ticker"] == "TSLA" for r in out["rows"])


def test_registry_tool_sets_last_rows_and_scopes_customer():
    from app.tools import registry
    ctx = registry.ToolContext(2001, 2025)
    out = asyncio.run(registry.invoke("sql_query", {"intent": "전체 거래"}, ctx))
    assert "error" not in out and ctx.last_rows is not None
    assert {r["customer_id"] for r in out["rows"]} == {2001} and out["customer_filter_injected"]
