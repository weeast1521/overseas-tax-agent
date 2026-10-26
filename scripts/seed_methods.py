"""데모용: 합성 고객 중 NH(나무) 계좌의 절반에 고객 방식을 저장한다 (SCRUM-47). 평가(eval)는 이 값을 쓰지 않는다(METHOD_DB=:memory:).

  python scripts/seed_methods.py [--db data/method.db]

결정론: seed 42. 결과 목록은 data/synthetic/customer_method.json에도 남긴다(합성 데이터라 커밋 가능).
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "method.db"))
    ap.add_argument("--tax-year", type=int, default=2025)
    args = ap.parse_args()
    os.environ["METHOD_DB"] = args.db
    from app import method_store
    trades = json.loads((ROOT / "data" / "synthetic" / "trades.json").read_text(encoding="utf-8"))
    accounts = sorted({(int(t["customer_id"]), t["account_id"]) for t in trades if t["broker_code"] == "NAMUH"})
    rnd = random.Random(42)
    picked = [a for a in accounts if rnd.random() < 0.5]
    rows = [{"customer_id": c, "account_id": a, "tax_year": args.tax_year, "method": rnd.choice(["FIFO", "AVG"]), "confirmed_by": "seed"} for c, a in picked]
    store = method_store.get_store()
    for r in rows:
        store.set(r["customer_id"], r["account_id"], r["tax_year"], r["method"], r["confirmed_by"], rule="free")
    (ROOT / "data" / "synthetic" / "customer_method.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"NAMUH 계좌 {len(accounts)}개 중 {len(rows)}개 저장 → {args.db}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
