"""F03-6 agent_trace 저장 — 민원 1건의 판단·스텝·결과를 남긴다. 상담원 "근거 보기"(F09)와 평가(F10)가 읽는다.

지금은 SQLite(파일, TRACE_DB=data/trace.db, gitignore). S2에서 PostgreSQL/Oracle로 교체해도 인터페이스는 같다.
저장 실패는 응답을 막지 않는다 — 기록은 best-effort, 민원 처리가 우선. 단 실패는 로그에 남긴다.

agent_trace  : 민원 1건 = 1행 (trace_id, 고객·연도, Judge 결과, status, 모델, 합계 토큰·지연)
agent_step   : 스텝 1개 = 1행 (thought, tool, args, observation, 지연, 토큰, 에러, off_plan)
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger(__name__)

DDL = """
CREATE TABLE IF NOT EXISTS agent_trace (
  trace_id      TEXT PRIMARY KEY,
  created_at    TEXT NOT NULL,
  customer_id   INTEGER NOT NULL,
  tax_year      INTEGER NOT NULL,
  inquiry_text  TEXT NOT NULL,
  category      TEXT NOT NULL,
  confidence    REAL NOT NULL,
  gate          TEXT,
  status        TEXT NOT NULL,
  review_reason TEXT,
  error_code    TEXT,
  aborted       TEXT,
  judge_name    TEXT,
  llm_name      TEXT,
  llm_model     TEXT,
  steps         INTEGER NOT NULL DEFAULT 0,
  tokens_in     INTEGER NOT NULL DEFAULT 0,
  tokens_out    INTEGER NOT NULL DEFAULT 0,
  latency_ms    INTEGER NOT NULL DEFAULT 0,
  final_text    TEXT
);
CREATE INDEX IF NOT EXISTS ix_agent_trace_status ON agent_trace(status, created_at);
CREATE TABLE IF NOT EXISTS agent_step (
  trace_id     TEXT NOT NULL REFERENCES agent_trace(trace_id),
  step_no      INTEGER NOT NULL,
  thought      TEXT,
  tool         TEXT,
  args_json    TEXT,
  observation  TEXT,
  latency_ms   INTEGER NOT NULL DEFAULT 0,
  in_tokens    INTEGER NOT NULL DEFAULT 0,
  out_tokens   INTEGER NOT NULL DEFAULT 0,
  error_code   TEXT,
  off_plan     INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (trace_id, step_no)
);
"""

STEP_COLS = ["step_no", "thought", "tool", "args_json", "observation", "latency_ms", "in_tokens", "out_tokens", "error_code", "off_plan", "tool_ms"]
# 기존 DB 호환: 없는 열은 열어볼 때 추가
_MIGRATIONS = [("agent_step", "tool_ms", "INTEGER NOT NULL DEFAULT 0"), ("agent_trace", "phases_json", "TEXT")]


class TraceStore(Protocol):
    def save(self, header: dict[str, Any], steps: list[dict[str, Any]]) -> None: ...
    def get(self, trace_id: str) -> dict[str, Any] | None: ...
    def list(self, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]: ...


class SQLiteTraceStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.executescript(DDL)
            for table, col, typ in _MIGRATIONS:
                if col not in {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})")}:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")

    def save(self, header: dict[str, Any], steps: list[dict[str, Any]]) -> None:
        h = {**header}
        h.setdefault("created_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
        cols = ["trace_id", "created_at", "customer_id", "tax_year", "inquiry_text", "category", "confidence", "gate", "status",
                "review_reason", "error_code", "aborted", "judge_name", "llm_name", "llm_model", "steps", "tokens_in", "tokens_out",
                "latency_ms", "final_text", "phases_json"]
        with self._lock, self._conn:
            self._conn.execute(f"INSERT OR REPLACE INTO agent_trace ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                               [h.get(c) for c in cols])
            self._conn.execute("DELETE FROM agent_step WHERE trace_id = ?", (h["trace_id"],))
            self._conn.executemany(
                f"INSERT INTO agent_step (trace_id,{','.join(STEP_COLS)}) VALUES (?,{','.join('?' * len(STEP_COLS))})",
                [[h["trace_id"]] + [s.get(c, 0 if c == "tool_ms" else None) for c in STEP_COLS] for s in steps])

    def get(self, trace_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM agent_trace WHERE trace_id = ?", (trace_id,)).fetchone()
            if row is None:
                return None
            steps = self._conn.execute("SELECT * FROM agent_step WHERE trace_id = ? ORDER BY step_no", (trace_id,)).fetchall()
        out = dict(row)
        out["steps_detail"] = [_step_out(dict(s)) for s in steps]
        return out

    def list(self, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        q = "SELECT trace_id, created_at, customer_id, category, confidence, status, review_reason, error_code, steps, latency_ms FROM agent_trace"
        args: list[Any] = []
        if status:
            q += " WHERE status = ?"
            args.append(status)
        q += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            return [dict(r) for r in self._conn.execute(q, args).fetchall()]


def _step_out(s: dict[str, Any]) -> dict[str, Any]:
    s["args"] = json.loads(s.pop("args_json")) if s.get("args_json") else None
    s["off_plan"] = bool(s.get("off_plan"))
    s.pop("trace_id", None)
    return s


def steps_from_agent(result: Any) -> list[dict[str, Any]]:
    """react.AgentResult.steps → agent_step 행."""
    return [{
        "step_no": st.step_no, "thought": st.thought, "tool": st.tool,
        "args_json": json.dumps(st.args, ensure_ascii=False) if st.args is not None else None,
        "observation": st.observation, "latency_ms": st.latency_ms, "in_tokens": st.in_tokens, "out_tokens": st.out_tokens,
        "error_code": st.error_code, "off_plan": int(bool(st.off_plan)), "tool_ms": getattr(st, "tool_ms", 0),
    } for st in (result.steps if result else [])]


_store: TraceStore | None = None


def get_store() -> TraceStore:
    global _store
    if _store is None:
        _store = SQLiteTraceStore(os.getenv("TRACE_DB", "data/trace.db"))
    return _store


def reset_store() -> None:  # 테스트용
    global _store
    _store = None


def save_safely(header: dict[str, Any], steps: list[dict[str, Any]]) -> bool:
    try:
        get_store().save(header, steps)
        return True
    except Exception:  # 기록 실패가 민원 처리를 막으면 안 된다
        log.exception("agent_trace save failed trace_id=%s", header.get("trace_id"))
        return False
