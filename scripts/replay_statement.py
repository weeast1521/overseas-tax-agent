"""확장판 명세서(달러 단가·환율 포함)로 우리 계산기를 실데이터에 대 본다 (맥에서만, SCRUM-1/2).

    python scripts/replay_statement.py data/mine/mirae_trades_2025_final.csv --expect-gain -1574687

세 가지를 본다. 출력은 건수와 합계 차이뿐 — 계좌번호·종목·날짜·건별 금액은 나오지 않는다.
  1) 환율 재계산: 줄마다 수량×달러단가×환율 이 명세서 원화와 같은가 (환율 기준일·반올림 규칙 확인)
  2) lot 매칭: 명세서 lot 을 거래로 되돌려 우리 FIFO 에 넣었을 때, 매도마다 같은 취득일 lot 을 고르는가
  3) 금액: 우리 계산기의 양도소득금액이 명세서(=계산내역서)와 몇 원 차이인가
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.importers import statement  # noqa: E402
from app.tools.tax_calc import CalcRequest, calculate  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--year", type=int, default=2025)
    ap.add_argument("--method", default="FIFO", choices=["FIFO", "AVG"])
    ap.add_argument("--expect-gain", type=int)
    a = ap.parse_args()

    lots = statement.load(a.path)
    t = statement.totals(lots)
    print(f"명세서: {t['rows']}줄 · 종목 {t['securities']}개 · 매도 {t['sales']}건 · 양도소득금액 {t['gain_krw']:,}")
    if not statement.has_usd(lots):
        print("달러 단가·환율 열이 없는 명세서입니다 — 확장판(매도단가/매수단가/매도일환율/매수일환율)을 넣어 주세요.")
        sys.exit(2)

    # 1) 환율 재계산
    fx = statement.fx_recompute(lots)
    for side, label in (("sell", "양도가액"), ("buy", "취득가액")):
        r = fx[side]
        print(f"1) {label}: 수량×달러단가×환율({r['rule']}) 이 ±1원 안에서 맞는 줄 {r['match']}/{fx['rows']}  · 합계 차이 {r['sum_diff_krw']:+,}원")

    # 2)·3) 계산기 재생
    trades = statement.to_trades(lots)
    res = calculate(CalcRequest(customer_id=1, tax_year=a.year, trades=trades, account_methods={"MIRAE-001": a.method}))
    want: dict[tuple, Counter] = defaultdict(Counter)
    for l in lots:
        want[(l.ticker or l.isin or l.name, l.sell_date)][l.acq_date] += l.qty
    got: dict[tuple, Counter] = defaultdict(Counter)
    for acc in res.by_account:
        for s in acc.sales:
            for u in s.lots_used:
                got[(s.ticker, s.settle_date)][u.buy_date] += u.qty
    same = sum(1 for k in want if want[k] == got.get(k))
    print(f"2) lot 매칭({a.method}): 매도 {len(want)}건 중 명세서와 같은 취득일·수량 조합 {same}건, 다른 건 {len(want) - same}")
    gain = res.total_gain_krw
    exp = a.expect_gain if a.expect_gain is not None else t["gain_krw"]
    print(f"3) 계산기 양도소득금액 {gain:,}  vs 계산내역서 {exp:,}  → 차이 {gain - exp:+,}원")
    print(f"   과세표준 {res.taxable_krw:,} · 예상 세액 {res.tax_krw:,}")
    if gain != exp:
        print("   차이가 나면: 1)의 불일치 줄 수가 0이 아닌지(환율·반올림), 2)의 다른 건이 있는지(방식), 남은 몇 원은 줄별 반올림 누적")


if __name__ == "__main__":
    main()
