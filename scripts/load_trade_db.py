"""거래 JSON → 읽기 전용 SQLite 거래 DB (F04, SCRUM-27).

    python scripts/load_trade_db.py                                   # TRADES_FILE(기본 data/samples/trades.json) → data/trade.db
    python scripts/load_trade_db.py --src data/mine/trades.json       # 내 데이터 (data/trade.db 는 .gitignore)
    python scripts/load_trade_db.py --src data/synthetic/trades.json --out data/trade_synth.db

실행기는 이 파일을 mode=ro 로 열고 PRAGMA query_only 를 켠다. TRADE_DB 가 없으면 TRADES_FILE 로 메모리 DB를 만들므로
이 스크립트는 선택 — 파일 DB 는 (1) 매 프로세스 로딩을 피하고 (2) 파일 권한까지 읽기 전용으로 둘 때 쓴다.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.tools.sql_exec import build_from_json  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=os.getenv("TRADES_FILE", "data/samples/trades.json"))
    ap.add_argument("--out", default=os.getenv("TRADE_DB", "data/trade.db"))
    ap.add_argument("--fx", default=None, help="환율 CSV(rate_date,usd_krw). 없으면 거래의 결제일 환율로 채움")
    a = ap.parse_args()
    n = build_from_json(a.src, a.out, a.fx)
    os.chmod(a.out, 0o444)  # 파일 권한도 읽기 전용
    print(f"{a.src} → {a.out}: 거래 {n}행 (읽기 전용). .env 에 TRADE_DB={a.out}")


if __name__ == "__main__":
    main()
