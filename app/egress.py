"""데이터 경계 (NF, SCRUM-51). 퍼블릭 LLM API 로 나가는 모든 요청은 여기를 지난다.

규칙: 실제 거래 데이터(data/mine/)는 어떤 경로로도 Anthropic API 로 나가지 않는다. 세 겹:
  (1) 구성 거부  — TRADES_FILE 이 data/mine/ 아래면 Claude 클라이언트를 만들 수 없다 (E-DATA-BOUNDARY)
  (2) 송신 검사  — 나가는 payload 를 보내기 전에 실데이터 지문(마커)·식별 정보 패턴으로 훑고, 걸리면 보내지 않는다
  (3) 단일 출구  — app/ 안에서 anthropic 클라이언트는 이 모듈로만 만든다 (tests/test_data_boundary.py 가 소스를 훑어 우회를 잡는다)

마커: 이 기계에 data/mine/trades.json 이 있으면 거기서 계좌·거래 ID 와 (종목, 수량, 단가) 조합을 뽑는다. 실데이터가 없는 기계(세션·CI)에서는
비어 있고, 그때도 계좌번호·주민번호 꼴 패턴은 막는다. 마커 자체는 메모리에만 있고 로그·응답에 쓰지 않는다.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]


class DataBoundaryError(RuntimeError):
    code = "E-DATA-BOUNDARY"


def assert_synthetic_data() -> None:
    """(1) 구성 거부. 실데이터로 돌리는 구성에서는 퍼블릭 LLM 클라이언트를 만들 수 없다."""
    src = os.getenv("TRADES_FILE", "")
    if src and "mine" in Path(src).parts:
        raise DataBoundaryError(f"TRADES_FILE={src} 은 실데이터. Claude API 구성에서는 data/samples 또는 합성 데이터만 쓴다")


# ---- (2) 송신 검사 ----
_PII_PATTERNS = [
    (re.compile(r"\b\d{6}-[1-4]\d{6}\b"), "주민등록번호 꼴"),
    (re.compile(r"\b\d{3}-\d{2,3}-\d{6,}\b"), "실계좌번호 꼴"),
]
_markers: dict[str, set[str]] | None = None


def mine_path() -> Path:
    return Path(os.getenv("MINE_TRADES_FILE", str(ROOT / "data" / "mine" / "trades.json")))


def _load_markers() -> dict[str, set[str]]:
    p = mine_path()
    out: dict[str, set[str]] = {"id": set(), "combo": set()}
    if not p.exists():
        return out
    try:
        rows = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return out
    for r in rows:
        for k in ("trade_id", "account_id"):
            v = str(r.get(k) or "")
            if len(v) >= 6:
                out["id"].add(v)
        if r.get("side") in ("BUY", "SELL") and r.get("qty") and r.get("price_usd"):
            out["combo"].add(f"{r.get('ticker')}|{_num(r['qty'])}|{_num(r['price_usd'])}")  # 종목·수량·단가 조합 — 합성과 우연히 겹칠 확률 무시 가능
    return out


def _num(v: Any) -> str:
    s = str(v)
    try:
        f = float(s)
        return str(int(f)) if f.is_integer() else f"{f:.6f}".rstrip("0").rstrip(".")
    except ValueError:
        return s


def markers() -> dict[str, set[str]]:
    global _markers
    if _markers is None:
        _markers = _load_markers()
    return _markers


def reset_markers() -> None:
    global _markers
    _markers = None


_COMBO_RE = re.compile(r"\b([A-Z]{1,6})\b\D{0,40}?(\d+(?:\.\d+)?)\D{0,40}?(\d+(?:\.\d+)?)")  # 종목 … 숫자 … 숫자 (사이에 말이 끼어도)


def scan(payload: Any) -> list[str]:
    """payload(어떤 구조든) 를 문자열로 펼쳐 실데이터 흔적을 찾는다. 걸린 사유 목록(비어 있으면 통과). 사유에 값 자체는 넣지 않는다."""
    blob = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, default=str)
    hits: list[str] = []
    m = markers()
    n_id = sum(1 for v in m["id"] if v in blob)
    if n_id:
        hits.append(f"실거래 식별자 {n_id}건")
    if m["combo"]:
        found = 0
        for t, a, b in _COMBO_RE.findall(blob):
            if f"{t}|{_num(a)}|{_num(b)}" in m["combo"] or f"{t}|{_num(b)}|{_num(a)}" in m["combo"]:
                found += 1
        if found:
            hits.append(f"실거래 종목·수량·단가 조합 {found}건")
    for pat, label in _PII_PATTERNS:
        if pat.search(blob):
            hits.append(label)
    return hits


# ---- 송신 기록 (테스트·e2e 가 본다) ----
_log: list[dict] = []
LOG_LIMIT = 500


def log() -> list[dict]:
    return _log


def _record(kind: str, kw: dict) -> None:
    if len(_log) >= LOG_LIMIT:
        del _log[: LOG_LIMIT // 2]
    _log.append({"kind": kind, "model": kw.get("model"), "system": kw.get("system"), "messages": kw.get("messages"), "tools": kw.get("tools")})


def check_outbound(kw: dict) -> None:
    hits = scan({k: kw.get(k) for k in ("system", "messages", "tools")})
    if hits:
        raise DataBoundaryError("외부 송신 차단: " + ", ".join(hits))


# ---- (3) 단일 출구 ----

def _guard_messages(obj: Any, kind: str, is_async: bool):
    orig = obj.messages.create
    if is_async:
        async def create(**kw):
            check_outbound(kw)
            _record(kind, kw)
            return await orig(**kw)
    else:
        def create(**kw):
            check_outbound(kw)
            _record(kind, kw)
            return orig(**kw)
    obj.messages.create = create
    return obj


def async_anthropic(kind: str = "llm", **kwargs):
    """AsyncAnthropic 을 경계 검사가 붙은 채로 만든다. app/ 안의 모든 비동기 호출은 이것으로."""
    assert_synthetic_data()
    from anthropic import AsyncAnthropic
    return _guard_messages(AsyncAnthropic(**kwargs), kind, is_async=True)


def anthropic(kind: str = "llm", **kwargs):
    """동기 버전 (질문 재작성·평가 채점 등)."""
    assert_synthetic_data()
    from anthropic import Anthropic
    return _guard_messages(Anthropic(**kwargs), kind, is_async=False)


def guard(client: Any, kind: str = "llm", is_async: bool = True):
    """이미 만들어진 클라이언트(테스트의 가짜 포함)에 검사를 붙인다."""
    return _guard_messages(client, kind, is_async)
