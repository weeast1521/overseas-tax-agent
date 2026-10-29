"""F04-6 Oracle 방언 (SCRUM-28). 실 Oracle 없이 — 파싱·변환·검사·few-shot 만. 컨테이너 검증은 SCRUM-48."""
import asyncio

import pytest
import sqlglot

from app.tools import sql_exec, sql_guard, sql_query


@pytest.fixture
def oracle(monkeypatch):
    monkeypatch.setenv("TRADE_DB_KIND", "oracle")
    sql_exec.reset_executor()


def test_dialect_from_env(oracle):
    assert sql_exec.dialect() == "oracle"


def test_oracle_row_cap_uses_fetch_first():
    g = sql_guard.bind_customer("SELECT * FROM trade WHERE customer_id = :customer_id", "oracle")
    assert g.sql.endswith("FETCH FIRST 500 ROWS ONLY") and "LIMIT" not in g.sql and g.limit_added
    g2 = sql_guard.bind_customer("SELECT * FROM trade WHERE customer_id = :customer_id FETCH FIRST 9999 ROWS ONLY", "oracle")
    assert "FETCH FIRST 500 ROWS ONLY" in g2.sql and "9999" not in g2.sql
    g3 = sql_guard.bind_customer("SELECT * FROM trade WHERE customer_id = :customer_id FETCH FIRST 10 ROWS ONLY", "oracle")
    assert "FETCH FIRST 10 ROWS ONLY" in g3.sql and not g3.limit_added


def test_oracle_alias_without_as_and_customer_wrap():
    g = sql_guard.bind_customer("SELECT t.ticker, a.broker_code FROM trade t JOIN account a ON a.account_id = t.account_id", "oracle")
    assert " AS t" not in g.sql and ") t JOIN (" in g.sql and g.sql.count("customer_id = :customer_id") == 2


@pytest.mark.parametrize("sql,reason", [
    ("SELECT DBMS_LOCK.SLEEP(10) FROM trade", "FORBIDDEN_FUNC"),
    ("SELECT UTL_HTTP.REQUEST('http://x') FROM trade", "FORBIDDEN_FUNC"),
    ("SELECT UTL_FILE.FOPEN('d', 'f', 'r') FROM trade", "FORBIDDEN_FUNC"),
    ("SELECT 1 FROM dual", "TABLE_NOT_ALLOWED"),
    ("SELECT * FROM trade WHERE customer_id = 1042", "CUSTOMER_LITERAL"),
    ("DELETE FROM trade", "NOT_SELECT"),
])
def test_oracle_malicious_rejected(sql, reason):
    with pytest.raises(sql_guard.SqlScopeError) as e:
        sql_guard.bind_customer(sql, "oracle")
    assert e.value.reason == reason


def test_unknown_dialect_rejected():
    with pytest.raises(sql_guard.SqlScopeError) as e:
        sql_guard.validate("SELECT * FROM trade", "mysql")
    assert e.value.reason == "DIALECT"


def test_oracle_fewshots_parse_and_use_date_literals():
    shots = sql_query.fewshot("oracle")
    assert len(shots) == len(sql_query.FEWSHOT)
    for intent, sql in shots:
        sqlglot.parse_one(sql, read="oracle")  # 파싱 성공
        g = sql_guard.bind_customer(sql, "oracle")
        assert g.tables <= sql_guard.ALLOWED_TABLES and ":customer_id" in g.sql, intent
        assert "LIMIT" not in g.sql and " AS t " not in g.sql, intent
        if "2025" in sql:
            assert "DATE '2025-01-01'" in sql and "'2025-01-01' AND" not in sql.replace("DATE '2025-01-01' AND", ""), intent  # 문자열 비교 금지
    assert any("TO_CHAR(settle_date, 'YYYY')" in sql for _, sql in shots)
    # postgres 예시는 그대로
    assert sql_query.fewshot("postgres") is sql_query.FEWSHOT


def test_oracle_schema_doc_mentions_fetch_and_date():
    doc = sql_query.schema_doc("oracle")
    assert "FETCH FIRST" in doc and "DATE '2025-01-01'" in doc and "TO_CHAR" in doc
    assert "FETCH FIRST" not in sql_query.schema_doc("sqlite")


def test_oracle_template_is_transpiled(oracle):
    sql = sql_query.template_sql("2025년 엔비디아 매도와 환율", ["NVDA"], 2025, "oracle")
    assert "fx_rate f" in sql and " AS f" not in sql and "ticker IN ('NVDA')" in sql
    g = sql_guard.bind_customer(sql, "oracle")
    assert g.sql.endswith("FETCH FIRST 500 ROWS ONLY")


def test_oracle_executor_requires_driver_or_env(oracle, monkeypatch):
    """oracledb 미설치 또는 DSN 없음 → 명확한 예외 (조용히 SQLite 로 떨어지지 않는다)."""
    monkeypatch.delenv("ORACLE_RO_DSN", raising=False)
    with pytest.raises((ImportError, KeyError)):
        sql_exec.get_executor()


def test_run_sql_query_oracle_path_reaches_executor(oracle, monkeypatch):
    """생성 → 검사(oracle) → 실행기까지. 실행기는 가짜로 바꿔 넘어온 SQL 이 Oracle 문법인지 본다."""
    seen = {}

    class Fake:
        def execute(self, sql, customer_id):
            seen["sql"], seen["cid"] = sql, customer_id
            return {"rows": [], "row_count": 0, "truncated": False, "columns": [], "elapsed_ms": 1}

    monkeypatch.setattr(sql_exec, "_exec", Fake())
    out = asyncio.run(sql_query.run_sql_query(None, "전체 거래", None, 2001, 2025))
    assert out["generated_by"] == "template" and seen["cid"] == 2001
    assert seen["sql"].endswith("FETCH FIRST 500 ROWS ONLY") and "(SELECT * FROM trade WHERE customer_id = :customer_id) trade" in seen["sql"]
