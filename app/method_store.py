"""F11-2 고객 산정 방식 저장 (SCRUM-47) — 선택제 증권사(나무) 계좌에서 고객이 고른 방식.

흐름: 기록 없음 → FIFO로 계산 + REVIEW/METHOD_UNCONFIRMED → 상담원이 고객에게 묻고 저장 → 다음 민원부터 자동.
method_override(요청 파라미터)는 저장 없는 1회성 재계산용으로 따로 둔다 (10/21 결정: 테이블 관리 + override 분리).

변경 규칙 (METHOD_CHANGE_RULE):
  once   (기본) 계좌·과세연도당 한 번 정하면 다른 방식으로 바꿀 수 없음 → E-METHOD-CHANGE. 같은 값 재저장은 허용(멱등)
  free   제한 없음
  ※ NH(나무) 원문 규칙은 SCRUM-3(본인 확인) 결과로 확정. 확인 전까지 보수적으로 once.

지금은 SQLite(METHOD_DB=data/method.db, gitignore). S2에서 Oracle로 바꿔도 인터페이스는 같다 (db/schema.sql).
"""
from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

DDL = """
CREATE TABLE IF NOT EXISTS customer_method (
  customer_id   INTEGER NOT NULL,
  account_id    TEXT    NOT NULL,
  tax_year      INTEGER NOT NULL,
  method        TEXT    NOT NULL CHECK (method IN ('FIFO', 'AVG')),
  changed_at    TEXT    NOT NULL,
  confirmed_by  TEXT    NOT NULL,
  PRIMARY KEY (customer_id, account_id, tax_year)
);
"""


class MethodChangeError(Exception):
    code = "E-METHOD-CHANGE"

    def __init__(self, account_id: str, tax_year: int, current: str, requested: str):
        super().__init__(f"{account_id} {tax_year}년 방식은 이미 {current}로 확정됨 — {requested}로 변경 불가 (1회 규칙)")
        self.account_id, self.tax_year, self.current, self.requested = account_id, tax_year, current, requested


class MethodStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.executescript(DDL)

    def get(self, customer_id: int, tax_year: int) -> dict[str, dict]:
        """account_id → {method, changed_at, confirmed_by}"""
        with self._lock:
            rows = self._conn.execute("SELECT * FROM customer_method WHERE customer_id = ? AND tax_year = ?",
                                      (customer_id, tax_year)).fetchall()
        return {r["account_id"]: {"method": r["method"], "changed_at": r["changed_at"], "confirmed_by": r["confirmed_by"]} for r in rows}

    def set(self, customer_id: int, account_id: str, tax_year: int, method: str, confirmed_by: str,
            rule: str | None = None) -> dict:
        rule = rule or os.getenv("METHOD_CHANGE_RULE", "once")
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock, self._conn:  # 조회와 기록을 한 트랜잭션으로 — 동시에 두 상담원이 저장해도 규칙이 깨지지 않게
            cur = self._conn.execute("SELECT method FROM customer_method WHERE customer_id = ? AND account_id = ? AND tax_year = ?",
                                     (customer_id, account_id, tax_year)).fetchone()
            if cur is not None and cur["method"] == method:
                return {"account_id": account_id, "tax_year": tax_year, "method": method, "changed": False}
            if cur is not None and rule == "once":
                raise MethodChangeError(account_id, tax_year, cur["method"], method)
            self._conn.execute("INSERT OR REPLACE INTO customer_method VALUES (?, ?, ?, ?, ?, ?)",
                               (customer_id, account_id, tax_year, method, now, confirmed_by))
        return {"account_id": account_id, "tax_year": tax_year, "method": method, "changed": True, "changed_at": now}


_store: MethodStore | None = None


def get_store() -> MethodStore:
    global _store
    if _store is None:
        _store = MethodStore(os.getenv("METHOD_DB", "data/method.db"))
    return _store


def reset_store() -> None:  # 테스트용
    global _store
    _store = None
