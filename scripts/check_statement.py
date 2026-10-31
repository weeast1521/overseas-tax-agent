"""증권사 양도소득세 계산명세서 점검 — 합계가 계산내역서와 맞는지, 증권사가 실제로 쓴 방식이 무엇인지 (맥에서만).

    python scripts/check_statement.py data/mine/mirae_trades_2025.csv --expect-gain -1574687 --expect-proceeds 71127628

출력은 집계 숫자와 건수만 — 종목명·ISIN·날짜·건별 금액은 나오지 않으므로 붙여 넣어도 된다.
(합계 금액은 출력된다: 본인이 이미 공유한 계산내역서 합계와 대조하기 위함.)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.importers import statement  # noqa: E402
from app.repo import BROKER_METHOD  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--broker", default="MIRAE")
    ap.add_argument("--expect-gain", type=int, help="계산내역서 양도소득금액")
    ap.add_argument("--expect-proceeds", type=int, help="계산내역서 양도가액합계")
    ap.add_argument("--expect-cost", type=int, help="계산내역서 취득가액합계")
    ap.add_argument("--expect-expenses", type=int, help="계산내역서 필요경비")
    a = ap.parse_args()

    lots = statement.load(a.path)
    t = statement.totals(lots)
    print(f"명세서: {t['rows']}줄 · 종목 {t['securities']}개 · 매도 {t['sales']}건")
    checks = [("양도가액", "proceeds_krw", a.expect_proceeds), ("취득가액", "cost_krw", a.expect_cost),
              ("필요경비", "expenses_krw", a.expect_expenses), ("양도소득금액", "gain_krw", a.expect_gain)]
    for label, k, exp in checks:
        line = f"  {label:<6} {t[k]:>15,}"
        if exp is not None:
            d = t[k] - exp
            line += f"   계산내역서 {exp:>15,}   차이 {d:>+,}  {'✓' if d == 0 else '✗'}"
        print(line)

    m = statement.method_signature(lots)
    print(f"\n산정 방식 흔적: 여러 lot 으로 쪼개진 매도 {m['sales_split_into_lots']}/{m['sales_total']}건, "
          f"선입선출과 모순 없는 종목 {m['securities_fifo_consistent']}개 / 모순 {m['securities_fifo_violated']}개, "
          f"취득일이 양도일보다 늦은 줄 {m['acq_after_sell_rows']}")
    table = BROKER_METHOD.get(a.broker.upper())
    print(f"→ 추정: {m['guess']}   (repo.BROKER_METHOD[{a.broker.upper()}] = {table})")
    if m["acq_after_sell_rows"]:
        print("  ⚠ 취득일 > 양도일: 이동평균에서 '최종 매수일'을 취득일로 적는 증권사가 있다 — AVG 신호")


if __name__ == "__main__":
    main()
