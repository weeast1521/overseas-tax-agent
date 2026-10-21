"""2일차: 내 세액 계산 + 증권사 계산내역서와 대조.

  python scripts/my_tax.py [--year 2025] [--trades data/mine/trades.json]
                           [--method NAMUH-001=AVG]           # 나무처럼 선택제 계좌의 방식 (기본 FIFO)
                           [--expect MIRAE-001=1234567]      # 그 계좌 계산내역서의 '양도소득금액' (선택)
                           [--lots]

계산 단위는 계좌다. 각 계좌 결과 = 그 증권사 계산내역서 한 장. 합산 = 계좌 결과를 더해 250만 공제 → 22%.
--expect로 계좌별 양도소득금액을 주면 계산내역서와의 차이를 보여준다. 차이가 나면 원인은
 (1) 방식(FIFO/AVG) 불일치 (2) 이체 입고 주식의 취득가액 (3) 환율 기준일 (4) 수수료 포함 방식 중 하나.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.repo import BROKER_METHOD  # noqa: E402
from app.tools.tax_calc import CalcError, CalcRequest, Trade, calculate  # noqa: E402


def won(n) -> str:
    return f"{int(n):,}원"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, default=2025)
    ap.add_argument("--trades", default="data/mine/trades.json")
    ap.add_argument("--method", action="append", default=[], help="ACCOUNT=FIFO|AVG")
    ap.add_argument("--expect", action="append", default=[], help="ACCOUNT=KRW (계산내역서 양도소득금액)")
    ap.add_argument("--lots", action="store_true")
    a = ap.parse_args()

    rows = json.loads(Path(a.trades).read_text(encoding="utf-8"))
    trades = [Trade(**{k: v for k, v in r.items() if k != "customer_id"}) for r in rows]

    methods = {}
    for t in trades:
        m = BROKER_METHOD.get(t.broker_code, "FIFO")
        methods[t.account_id] = "FIFO" if m == "CHOICE" else m
    for spec in a.method:
        k, v = spec.split("=")
        methods[k] = v

    try:
        res = calculate(CalcRequest(customer_id=1, tax_year=a.year, trades=trades, account_methods=methods))
    except CalcError as e:
        print(f"{e.code}: {e}")
        if e.code == "E-CALC-SHORT":
            print("→ 이 계좌에 매수 lot이 부족합니다. 다른 증권사에서 이체(대체입고)한 주식이면 TRANSFER_OUT/IN 두 행을 추가하고,")
            print("  아니면 CSV 내보내기 기간을 더 앞으로 늘리세요.")
        sys.exit(2)

    expect = {k: int(v) for k, v in (s.split("=") for s in a.expect)}

    for acc in res.by_account:
        print(f"\n== {acc.account_id} ({acc.broker_code}, {acc.method}) — 이 증권사 계산내역서에 해당 ==")
        print(f"{'종목':8} {'매도일':>10} {'수량':>6} {'양도가액':>14} {'취득가액':>14} {'양도소득금액':>14} {'USD':>9}  비고")
        for s in acc.sales:
            note = "이체 lot 포함 → 받은 증권사 계산서와 다를 수 있음" if s.has_transferred_lot else ""
            print(f"{s.ticker:8} {s.settle_date!s:>10} {s.sold_qty:>6} {won(s.proceeds_krw):>14} {won(s.cost_krw):>14} {won(s.gain_krw):>14} {s.usd_gain:>9}  {note}")
            if a.lots:
                for l in s.lots_used:
                    tag = " (이체)" if l.transferred else ""
                    print(f"{'':8}   └ {l.origin_account_id} {l.buy_trade_id} {l.qty}주 @${l.unit_price_usd} {l.buy_date} fx {l.fx_buy} → {won(l.cost_krw)}{tag}")
        print(f"계좌 양도소득금액 합계: {won(acc.gain_krw)}")
        if acc.account_id in expect:
            diff = expect[acc.account_id] - acc.gain_krw
            print(f"계산내역서 {won(expect[acc.account_id])} vs 계산 {won(acc.gain_krw)} → 차이 {won(diff)}"
                  + ("" if diff == 0 else "  ← 방식·이체 취득가액·환율 기준일·수수료 중 하나"))

    print(f"\n== 납세자 단위 합산 ({a.year}년 귀속) ==")
    print(f"양도소득금액 합계 {won(res.total_gain_krw)} / 기본공제 {won(res.deduction_krw)} / 과세표준 {won(res.taxable_krw)} / 납부세액(22%) {won(res.tax_krw)}")
    print("세율·공제·환율 기준일은 국세청 2025년 귀속 안내로 확정 전. 신고 전 세무 전문가 확인 권장.")


if __name__ == "__main__":
    main()
