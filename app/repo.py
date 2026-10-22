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


def search_corpus(query: str, k: int = 5) -> list[dict]:
    chunks = []
    for p in sorted((DATA / "corpus").glob("*.json")):
        chunks.extend(json.loads(p.read_text(encoding="utf-8")))
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


def account_methods(customer_id: int, override: str | None = None) -> dict[str, str]:
    """계좌별 산정 방식. CHOICE 증권사는 customer_method가 있으면 그 값, 없으면 FIFO."""
    return resolve_methods(customer_id, override)["methods"]


def resolve_methods(customer_id: int, override: str | None = None) -> dict:
    """F11: 계좌별 증권사 기본 방식, 적용 방식, 확인 필요 계좌.

    brokers: account_id → 증권사 기본(FIFO|AVG|CHOICE)
    methods: account_id → 실제 계산에 쓰는 방식(FIFO|AVG)
    needs_confirmation: 선택제(CHOICE)인데 고객 선택값도 override도 없는 계좌 → F01은 REVIEW/METHOD_UNCONFIRMED
    customer_method 테이블은 SCRUM-47에서. 지금은 override만 본다.
    """
    brokers: dict[str, str] = {}
    methods: dict[str, str] = {}
    needs: list[str] = []
    for r in load_trades(customer_id):
        acc = r["account_id"]
        if acc in brokers:
            continue
        base = BROKER_METHOD.get(r["broker_code"], "FIFO")
        brokers[acc] = base
        if base == "CHOICE":
            if override:
                methods[acc] = override
            else:
                methods[acc] = "FIFO"
                needs.append(acc)
        else:
            methods[acc] = base
    return {"brokers": brokers, "methods": methods, "needs_confirmation": needs}
