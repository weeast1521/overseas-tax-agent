"""F04-4 읽기 전용 실행 (SCRUM-27). 세 겹 중 마지막 — 파서를 우회해도 여기서 막힌다.

S1(로컬): SQLite 거래 DB. TRADE_DB 경로(기본 data/trade.db). 없으면 TRADES_FILE(JSON)로 메모리 DB를 만든다(테스트·세션).
  - 파일은 mode=ro 로 열고 PRAGMA query_only=1 → INSERT/UPDATE는 DB가 거부
  - progress_handler 로 SQL_TIMEOUT_SEC(기본 5초) 초과 시 중단 → E-SQL-EXEC
  - 결과 500행 상한, 초과면 truncated=true
S2: PgExecutor — PG_TRADE_RO_DSN(읽기 전용 계정, db/readonly_user.sql). 인터페이스 동일.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

from app.tools.sql_guard import MAX_ROWS

ROOT = Path(__file__).resolve().parents[2]
DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS customer (customer_id INTEGER PRIMARY KEY, display_name TEXT);
CREATE TABLE IF NOT EXISTS account (account_id TEXT PRIMARY KEY, customer_id INTEGER NOT NULL, broker_code TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS trade (
  trade_id TEXT PRIMARY KEY, customer_id INTEGER NOT NULL, account_id TEXT NOT NULL, broker_code TEXT NOT NULL,
  ticker TEXT NOT NULL, side TEXT NOT NULL CHECK (side IN ('BUY','SELL','TRANSFER_IN','TRANSFER_OUT')),
  qty TEXT NOT NULL, price_usd TEXT NOT NULL DEFAULT '0', fee_usd TEXT NOT NULL DEFAULT '0',
  trade_date TEXT NOT NULL, settle_date TEXT NOT NULL, fx_settle TEXT, transfer_id TEXT);
CREATE INDEX IF NOT EXISTS ix_trade_customer ON trade (customer_id, settle_date);
CREATE TABLE IF NOT EXISTS fx_rate (rate_date TEXT PRIMARY KEY, usd_krw TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settlement_daily (customer_id INTEGER NOT NULL, settle_date TEXT NOT NULL, account_id TEXT NOT NULL,
  buy_krw INTEGER NOT NULL DEFAULT 0, sell_krw INTEGER NOT NULL DEFAULT 0, fee_krw INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (customer_id, settle_date, account_id));
"""
# 숫자는 TEXT로 저장 — Decimal 정밀도 보존 (tax_calc가 Decimal로 읽는다). SQL의 SUM 등은 SQLite가 숫자로 변환해 계산한다.


class SqlExecError(Exception):
    code = "E-SQL-EXEC"


def load_rows_into(conn: sqlite3.Connection, trades: list[dict], fx: list[tuple[str, str]] | None = None) -> None:
    conn.executescript(DDL_SQLITE)
    custs = sorted({int(r["customer_id"]) for r in trades})
    conn.executemany("INSERT OR IGNORE INTO customer(customer_id) VALUES (?)", [(c,) for c in custs])
    accts = {(r["account_id"], int(r["customer_id"]), r["broker_code"]) for r in trades}
    conn.executemany("INSERT OR IGNORE INTO account VALUES (?,?,?)", sorted(accts))
    conn.executemany("INSERT OR REPLACE INTO trade VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     [(r["trade_id"], int(r["customer_id"]), r["account_id"], r["broker_code"], r["ticker"], r["side"], str(r["qty"]),
                       str(r.get("price_usd", 0)), str(r.get("fee_usd", 0)), r["trade_date"], r["settle_date"],
                       str(r["fx_settle"]) if r.get("fx_settle") is not None else None, r.get("transfer_id")) for r in trades])
    if fx:
        conn.executemany("INSERT OR REPLACE INTO fx_rate VALUES (?,?)", fx)
    else:  # 거래의 결제일 환율로 최소한의 fx_rate 를 만든다 (조인 예시가 돌게)
        conn.executemany("INSERT OR IGNORE INTO fx_rate VALUES (?,?)",
                         sorted({(r["settle_date"], str(r["fx_settle"])) for r in trades if r.get("fx_settle")}))
    conn.commit()


def build_from_json(trades_file: str | Path, db_path: str | Path, fx_csv: str | Path | None = None) -> int:
    """scripts/load_trade_db.py 가 호출. JSON → SQLite 파일."""
    trades = json.loads(Path(trades_file).read_text(encoding="utf-8"))
    fx = None
    if fx_csv and Path(fx_csv).exists():
        lines = Path(fx_csv).read_text(encoding="utf-8").splitlines()[1:]
        fx = [tuple(l.split(",")[:2]) for l in lines if l.strip()]
    p = Path(db_path)
    if p.exists():
        p.unlink()
    p.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(p) as conn:
        load_rows_into(conn, trades, fx)
    return len(trades)


class SqliteExecutor:
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or os.getenv("TRADE_DB")
        self.timeout = float(os.getenv("SQL_TIMEOUT_SEC", "5"))
        self._mem: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        if self.db_path and Path(self.db_path).exists():
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, check_same_thread=False)  # 파일 자체를 읽기 전용으로
        else:  # TRADES_FILE 로 메모리 DB (테스트·세션). 매 호출 새로 만들지 않도록 캐시
            if self._mem is None:
                src = os.getenv("TRADES_FILE", str(ROOT / "data" / "samples" / "trades.json"))
                self._mem = sqlite3.connect(":memory:", check_same_thread=False)
                load_rows_into(self._mem, json.loads(Path(src).read_text(encoding="utf-8")))
            conn = self._mem
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = 1")  # 쓰기는 DB 단계에서 거부 (파서 우회 대비)
        return conn

    def execute(self, sql: str, customer_id: int) -> dict:
        """세션 customer_id 만 바인드한다 — SQL 안의 다른 플레이스홀더는 허용하지 않는다."""
        conn = self._connect()
        deadline = time.perf_counter() + self.timeout
        conn.set_progress_handler(lambda: 1 if time.perf_counter() > deadline else 0, 10_000)  # 1 = 중단
        t0 = time.perf_counter()
        try:
            with self._lock:
                cur = conn.execute(sql, {"customer_id": customer_id})
                rows = cur.fetchmany(MAX_ROWS + 1)
                cols = [d[0] for d in cur.description] if cur.description else []
        except sqlite3.OperationalError as e:
            msg = str(e)
            if "interrupted" in msg:
                raise SqlExecError(f"쿼리 시간 초과({self.timeout:.0f}초)") from e
            raise SqlExecError(msg[:200]) from e
        except sqlite3.DatabaseError as e:
            raise SqlExecError(str(e)[:200]) from e
        finally:
            conn.set_progress_handler(None, 0)
            if conn is not self._mem:
                conn.close()
        out = [dict(zip(cols, r)) for r in rows[:MAX_ROWS]]
        return {"rows": out, "row_count": len(out), "truncated": len(rows) > MAX_ROWS, "columns": cols,
                "elapsed_ms": int((time.perf_counter() - t0) * 1000)}


class PgExecutor:
    """S2. 읽기 전용 계정 DSN(PG_TRADE_RO_DSN). statement_timeout 은 계정 설정(db/readonly_user.sql)."""

    def __init__(self, dsn: str | None = None):
        import psycopg  # noqa: F401
        self.dsn = dsn or os.environ["PG_TRADE_RO_DSN"]

    def execute(self, sql: str, customer_id: int) -> dict:
        import psycopg
        t0 = time.perf_counter()
        try:
            with psycopg.connect(self.dsn, autocommit=True) as conn, conn.cursor() as cur:
                cur.execute("SET TRANSACTION READ ONLY")
                cur.execute(sql, {"customer_id": customer_id})
                rows = cur.fetchmany(MAX_ROWS + 1)
                cols = [d.name for d in cur.description] if cur.description else []
        except Exception as e:
            raise SqlExecError(str(e)[:200]) from e
        out = [dict(zip(cols, r)) for r in rows[:MAX_ROWS]]
        return {"rows": out, "row_count": len(out), "truncated": len(rows) > MAX_ROWS, "columns": cols,
                "elapsed_ms": int((time.perf_counter() - t0) * 1000)}


_exec = None


def get_executor():
    global _exec
    if _exec is None:
        _exec = PgExecutor() if os.getenv("TRADE_DB_KIND", "sqlite") == "pg" else SqliteExecutor()
    return _exec


def reset_executor() -> None:
    global _exec
    _exec = None


def dialect() -> str:
    return "postgres" if os.getenv("TRADE_DB_KIND", "sqlite") == "pg" else "sqlite"
