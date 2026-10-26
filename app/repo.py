"""데이터 접근. 1~2일차는 파일(data/samples, data/corpus), 2일차 저녁부터 PostgreSQL로 교체.

load_trades(customer_id)  → tax_calc.Trade 호환 dict 목록
search_corpus(query, k)   → 규정 chunk 목록 (law, article, text, score)
"""
from __future__ import annotations

import json
import os
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"


def load_trades(customer_id: int, tickers: list[str] | None = None) -> list[dict]:
    src = os.getenv("TRADES_FILE", str(DATA / "samples" / "trades.json"))
    rows = json.loads(Path(src).read_text(encoding="utf-8"))
    rows = [r for r in rows if int(r["customer_id"]) == customer_id]
    if tickers:
        want = {t.upper() for t in tickers}
        rows = [r for r in rows if r["ticker"].upper() in want]
    return [{k: v for k, v in r.items() if k != "customer_id"} for r in rows]


def corpus_files() -> list[Path]:
    """규정 코퍼스 파일. CORPUS_DIR > data/corpus/v1 (SCRUM-31 원문 수집본) > data/corpus/seed.json (자리표시 7개)."""
    d = os.getenv("CORPUS_DIR")
    if d:
        return sorted(Path(d).glob("*.json"))
    v1 = sorted((DATA / "corpus" / "v1").glob("*.json"))
    return v1 or [DATA / "corpus" / "seed.json"]


def load_corpus() -> list[dict]:
    return [c for p in corpus_files() for c in json.loads(p.read_text(encoding="utf-8"))]


def search_corpus(query: str, k: int = 5) -> list[dict]:
    chunks = load_corpus()
    terms = [t for t in query.replace("·", " ").split() if len(t) > 1]
    scored = []
    for c in chunks:
        hay = f"{c.get('law','')} {c.get('article','')} {c.get('text','')} {' '.join(c.get('keywords', []))}"
        s = sum(1 for t in terms if t in hay)
        if s:
            scored.append({**c, "score": round(s / max(len(terms), 1), 2)})
    return sorted(scored, key=lambda c: -c["score"])[:k]


BROKER_METHOD = {  # 증권사 기본 산정 방식. 출처: 시사저널e 2025-04, 각사 FAQ. as_of 2025-04
    "MIRAE": "FIFO", "KIWOOM": "FIFO", "KB": "FIFO", "KAKAOPAY": "FIFO", "SHINHAN": "FIFO",
    "SAMSUNG": "AVG", "KIS": "AVG", "DAISHIN": "AVG", "TOSS": "AVG",
    "NAMUH": "CHOICE",  # NH투자증권: 고객 선택, 기본 FIFO
}


def account_methods(customer_id: int, override: str | None = None, tax_year: int | None = None) -> dict[str, str]:
    """계좌별 산정 방식. CHOICE 증권사는 override > customer_method > FIFO."""
    return resolve_methods(customer_id, override, tax_year)["methods"]


def resolve_methods(customer_id: int, override: str | None = None, tax_year: int | None = None) -> dict:
    """F11: 계좌별 증권사 기본 방식, 적용 방식, 확인 필요 계좌 (SCRUM-47).

    brokers:   account_id → 증권사 기본(FIFO|AVG|CHOICE)
    methods:   account_id → 실제 계산에 쓰는 방식(FIFO|AVG)
    confirmed: account_id → {method, changed_at, confirmed_by}  선택제 계좌 중 customer_method에 저장된 것
    needs_confirmation: 선택제인데 override도 저장값도 없는 계좌 → F01은 REVIEW/METHOD_UNCONFIRMED, 계산은 FIFO
    우선순위: method_override(1회성, 저장 안 함) > customer_method(상담원이 고객 확인 후 저장) > FIFO
    """
    from app import method_store
    tax_year = tax_year or int(os.getenv("TAX_YEAR", "2025"))
    stored = method_store.get_store().get(customer_id, tax_year)
    brokers: dict[str, str] = {}
    broker_codes: dict[str, str] = {}
    methods: dict[str, str] = {}
    confirmed: dict[str, dict] = {}
    needs: list[str] = []
    for r in load_trades(customer_id):
        acc = r["account_id"]
        if acc in brokers:
            continue
        base = BROKER_METHOD.get(r["broker_code"], "FIFO")
        brokers[acc] = base
        broker_codes[acc] = r["broker_code"]
        if base != "CHOICE":
            methods[acc] = base
        elif override:
            methods[acc] = override
        elif acc in stored:
            methods[acc] = stored[acc]["method"]
            confirmed[acc] = stored[acc]
        else:
            methods[acc] = "FIFO"
            needs.append(acc)
    return {"brokers": brokers, "broker_codes": broker_codes, "methods": methods, "confirmed": confirmed,
            "needs_confirmation": needs, "tax_year": tax_year}
