"""1일차: 증권사 CSV → 통합 → data/mine/trades.json

사용:
  python scripts/import_my_trades.py --mirae ~/Downloads/mirae_2025.csv --namuh ~/Downloads/namuh_2025.csv
  (2025년 매도분의 lot을 맞추려면 2024년 이전 매수분도 포함된 기간으로 내보낼 것)

출력 요약: 증권사별 건수, 종목별 순수량, 결제일 보정 건수. 결제일 누락 0이어야 2일차로.
실제 거래는 data/mine/ 에만 둔다 (gitignore). 퍼블릭 API로 보내지 않는다.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.importers import mirae, namuh  # noqa: E402
from app.importers.settle import finalize  # noqa: E402

MY_CUSTOMER_ID = 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mirae")
    ap.add_argument("--namuh")
    ap.add_argument("--out", default="data/mine/trades.json")
    a = ap.parse_args()

    raws = []
    if a.mirae:
        raws += mirae.load(a.mirae)
    if a.namuh:
        raws += namuh.load(a.namuh)
    if not raws:
        ap.error("--mirae 또는 --namuh 중 하나는 필요")

    missing_settle = sum(1 for r in raws if r.settle_date is None)
    trades = finalize(raws)

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps([{"customer_id": MY_CUSTOMER_ID, **json.loads(t.model_dump_json())} for t in trades],
                              ensure_ascii=False, indent=1), encoding="utf-8")

    by_broker = Counter(t.broker_code for t in trades)
    net = defaultdict(lambda: 0)
    for t in trades:
        net[t.ticker] += t.qty if t.side == "BUY" else -t.qty
    print(f"총 {len(trades)}건 (중복 제거 후)  증권사별 {dict(by_broker)}")
    print(f"결제일 CSV에 없어 T+2로 보정한 건수: {missing_settle}")
    print("종목별 순수량:", {k: str(v) for k, v in sorted(net.items())})
    neg = {k: v for k, v in net.items() if v < 0}
    if neg:
        print("⚠ 순수량 음수 종목 (이전 매수분 누락 가능):", neg)
    print(f"→ {out}")


if __name__ == "__main__":
    main()
