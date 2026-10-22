"""SCRUM-38 (F08-B): 합성 고객 300명·종목 15·증권사 10 거래 데이터 생성 — 결정론적(시드 고정), LLM 불필요.

  python scripts/gen_synthetic.py [--seed 42] [--customers 300] [--out data/synthetic]

왜 LLM이 아니라 코드로 만드나: 거래 내역은 "lot이 모자라지 않고, 이체 짝이 맞고, 결제일·환율이 규칙대로"여야 한다.
LLM이 만들면 그 제약을 지켰는지 또 검증해야 한다. 코드로 만들면 같은 시드 → 같은 데이터(재현), 비용 0.
LLM(Batch API)은 민원 "문장"을 만들 때 쓴다 (SCRUM-42 평가셋).

산출물 (data/synthetic/, git에 포함 — 합성이라 공개 가능):
  customers.json  고객 300명: 시나리오·계좌·증권사 (평가셋이 민원 문장을 붙일 때 참고)
  trades.json     tax_calc.Trade 호환 행 (TRADES_FILE로 바로 사용)
  fx_rate.csv     2024-01-02~2025-12-31 영업일 매매기준율 (합성 랜덤워크)
  expected.json   고객별 tax_calc 결과 요약 (정답지: tax_krw·total_gain·by_account·이체 포함 매도·needs_confirmation)

시나리오 (고객마다 1개 + 겹침 요소):
  SINGLE_FIFO   단일 증권사(FIFO)  SINGLE_AVG   단일 증권사(AVG)   MULTI_BROKER  두 증권사 각각 거래
  TRANSFER      A 매수 → B로 이체 → B에서 매도    NO_SALE 매수만(과세연도 매도 없음)    LOSS 손실 종목 포함(겹침)
  NAMUH 선택제 계좌 포함(겹침) → needs_confirmation 발생    CARRY 2024 매수분을 2025에 매도(겹침)
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from collections import Counter
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.importers.settle import load_holidays, us_settle  # noqa: E402
from app.repo import BROKER_METHOD  # noqa: E402
from app.tools import tax_calc  # noqa: E402

TICKERS = {  # 2024-01-02 기준 가격(USD)와 연간 추세(배수). 실제 시세가 아니라 합성 경로의 출발점
    "AAPL": (185, 1.25), "MSFT": (370, 1.20), "NVDA": (48, 2.80), "TSLA": (250, 1.05), "AMZN": (150, 1.35),
    "GOOGL": (140, 1.30), "META": (350, 1.55), "AMD": (140, 0.95), "PLTR": (16, 3.50), "COIN": (160, 1.40),
    "LCID": (4.2, 0.60), "RIVN": (22, 0.55), "INTC": (48, 0.45), "SOXL": (30, 0.80), "SCHD": (76, 1.08),
}
BROKERS = list(BROKER_METHOD)  # 10개
SCENARIOS = [("SINGLE_FIFO", 30), ("SINGLE_AVG", 15), ("MULTI_BROKER", 22), ("TRANSFER", 18), ("NO_SALE", 8), ("LOSS_HEAVY", 7)]
TAX_YEAR = 2025
SURNAMES = ["김", "이", "박", "최", "정", "강", "조", "윤", "장", "임", "한", "오", "서", "신", "권", "황", "안", "송", "류", "홍"]
GIVEN = ["민준", "서연", "도윤", "지우", "하준", "서현", "예준", "하은", "지호", "수아", "준서", "지민", "현우", "지아", "건우", "다은", "우진", "채원", "선우", "유나"]


def d2(x: float | Decimal) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def business_days(start: date, end: date, holidays: set[date]) -> list[date]:
    out, d = [], start
    while d <= end:
        if d.weekday() < 5 and d not in holidays:
            out.append(d)
        d += timedelta(days=1)
    return out


def gen_fx(rng: random.Random, days: list[date]) -> dict[date, Decimal]:
    fx, v = {}, 1300.0
    for d in days:
        v = max(1250.0, min(1500.0, v + rng.gauss(0.08, 3.2)))
        fx[d] = Decimal(str(round(v, 1)))
    return fx


def gen_prices(rng: random.Random, days: list[date]) -> dict[str, dict[date, Decimal]]:
    """종목별 가격 경로: 랜덤워크를 만든 뒤 끝점이 목표 배수(trend)에 맞도록 선형 보정(브리지). 중간 변동은 남고 2년 수익률은 목표대로."""
    import math
    out = {}
    n = len(days)
    for t, (p0, trend) in TICKERS.items():
        logs = [0.0]
        for _ in range(1, n):
            logs.append(logs[-1] + rng.gauss(0, 0.018))
        target = math.log(trend)
        adj = (target - logs[-1]) / (n - 1)
        out[t] = {d: d2(max(0.5, p0 * math.exp(l + adj * i))) for i, (d, l) in enumerate(zip(days, logs))}
    return out


class Gen:
    def __init__(self, seed: int):
        self.rng = random.Random(seed)
        self.holidays = load_holidays()
        self.days = business_days(date(2024, 1, 2), date(2025, 12, 31), self.holidays)
        self.days_2024 = [d for d in self.days if d.year == 2024]
        self.days_2025 = [d for d in self.days if d.year == TAX_YEAR and d <= date(2025, 12, 15)]
        self.fx = gen_fx(self.rng, business_days(date(2024, 1, 1), date(2025, 12, 31), set()))  # 한국 기준: 주말만 제외
        self.prices = gen_prices(self.rng, self.days)
        self.seq = 0

    def fx_for(self, settle: date) -> Decimal:
        d = settle
        while d not in self.fx:
            d -= timedelta(days=1)
        return self.fx[d]

    def row(self, cust: int, acc: str, broker: str, ticker: str, side: str, qty: int, td: date, transfer_id: str | None = None) -> dict:
        self.seq += 1
        if side.startswith("TRANSFER"):
            price, fee, settle, fx = Decimal("0"), Decimal("0"), td, Decimal("1")
        else:
            price = self.prices[ticker][td]
            fee = d2(price * qty * Decimal("0.0025"))
            settle = us_settle(td, self.holidays)
            fx = self.fx_for(settle)
        return {"customer_id": cust, "account_id": acc, "trade_id": f"{broker}-{td:%Y%m%d}-{self.seq:05d}", "broker_code": broker,
                "ticker": ticker, "side": side, "qty": str(qty), "price_usd": str(price), "fee_usd": str(fee),
                "trade_date": td.isoformat(), "settle_date": settle.isoformat(), "fx_settle": str(fx), "transfer_id": transfer_id}

    def pick_days(self, pool: list[date], k: int) -> list[date]:
        return sorted(self.rng.sample(pool, k))

    def buys_then_sells(self, cust, acc, broker, ticker, n_buy, n_sell, carry: bool, sell_all=False) -> list[dict]:
        """매수 n_buy회(일부 2024) 후 2025에 매도 n_sell회. 보유 수량을 넘겨 팔지 않는다."""
        rows, held = [], 0
        buy_pool = (self.days_2024 if carry else []) + self.days_2025[: len(self.days_2025) // 2]
        buy_days = self.pick_days(buy_pool, n_buy)
        for bd in buy_days:
            q = self.rng.choice([10, 15, 20, 30, 40, 50, 60, 80, 100, 150])
            rows.append(self.row(cust, acc, broker, ticker, "BUY", q, bd)); held += q
        if n_sell:
            later = [d for d in self.days_2025 if d > buy_days[-1]]
            if len(later) < n_sell:
                return rows
            for sd in self.pick_days(later, n_sell):
                q = held if sell_all else max(1, int(held * self.rng.choice([0.3, 0.5, 0.6, 1.0])))
                q = min(q, held)
                if q <= 0:
                    break
                rows.append(self.row(cust, acc, broker, ticker, "SELL", q, sd)); held -= q
        return rows

    def customer(self, cust: int, scenario: str) -> tuple[dict, list[dict]]:
        rng = self.rng
        rows: list[dict] = []
        carry = rng.random() < 0.45
        n_t = rng.choice([1, 1, 2, 2, 3])
        tickers = rng.sample(list(TICKERS), n_t)
        prof = {"customer_id": cust, "name": rng.choice(SURNAMES) + rng.choice(GIVEN),  # 가짜 이름 (F08-10). 주민번호·연락처 필드 없음
                "scenario": scenario, "accounts": [], "tickers": tickers, "carry_2024_lots": carry}

        def acc_for(broker):
            a = f"{broker}-{cust}"
            acct_no = f"{rng.randint(100, 999)}-{rng.randint(10, 99)}-{rng.randint(100000, 999999)}"  # 형식만 흉내낸 가짜 계좌번호
            prof["accounts"].append({"account_id": a, "broker_code": broker, "method": BROKER_METHOD[broker], "display_no": acct_no})
            return a

        if scenario in ("SINGLE_FIFO", "SINGLE_AVG", "NO_SALE", "LOSS_HEAVY"):
            pool = [b for b in BROKERS if BROKER_METHOD[b] == ("AVG" if scenario == "SINGLE_AVG" else "FIFO")]
            if scenario == "LOSS_HEAVY":
                tickers = rng.sample(["LCID", "RIVN", "INTC", "SOXL"], min(2, n_t)) + tickers[: max(0, n_t - 2)]
                prof["tickers"] = tickers
            broker = rng.choice(pool + (["NAMUH"] if rng.random() < 0.25 else []))
            acc = acc_for(broker)
            for t in tickers:
                rows += self.buys_then_sells(cust, acc, broker, t, rng.choice([1, 2, 3]), 0 if scenario == "NO_SALE" else rng.choice([1, 1, 2]), carry)
        elif scenario == "MULTI_BROKER":
            b1, b2 = rng.sample(BROKERS, 2)
            a1, a2 = acc_for(b1), acc_for(b2)
            for t in tickers:
                rows += self.buys_then_sells(cust, a1, b1, t, rng.choice([1, 2]), 1, carry)
                rows += self.buys_then_sells(cust, a2, b2, t if rng.random() < 0.5 else rng.choice(list(TICKERS)), rng.choice([1, 2]), 1, carry)
        elif scenario == "TRANSFER":
            b_from, b_to = rng.sample(BROKERS, 2)
            if rng.random() < 0.4:
                b_to = "NAMUH" if b_from != "NAMUH" else "TOSS"
            a_from, a_to = acc_for(b_from), acc_for(b_to)
            t = tickers[0]
            prof["transfer"] = {"from": a_from, "to": a_to, "ticker": t}
            buy_days = self.pick_days((self.days_2024 if carry else []) + self.days_2025[:60], rng.choice([1, 2]))
            held = 0
            for bd in buy_days:
                q = rng.choice([20, 30, 50, 80, 100]); rows.append(self.row(cust, a_from, b_from, t, "BUY", q, bd)); held += q
            tday = rng.choice([d for d in self.days_2025 if d > buy_days[-1] and d < date(2025, 9, 1)])
            tq = held if rng.random() < 0.6 else max(1, held // 2)
            tid = f"tr-{cust}-1"
            rows.append(self.row(cust, a_from, b_from, t, "TRANSFER_OUT", tq, tday, tid))
            rows.append(self.row(cust, a_to, b_to, t, "TRANSFER_IN", tq, tday, tid))
            last = tday
            if rng.random() < 0.5:  # 받은 쪽에서 추가 매수 → 이체 lot과 자체 매수 lot이 섞인다
                bd2 = rng.choice([d for d in self.days_2025 if tday < d < date(2025, 9, 15)])
                q2 = rng.choice([10, 20, 30])
                rows.append(self.row(cust, a_to, b_to, t, "BUY", q2, bd2)); tq += q2; last = bd2
            sday = rng.choice([d for d in self.days_2025 if d > last + timedelta(days=15)])
            rows.append(self.row(cust, a_to, b_to, t, "SELL", max(1, int(tq * rng.choice([0.5, 1.0]))), sday))
            for t2 in tickers[1:]:
                rows += self.buys_then_sells(cust, a_from, b_from, t2, 1, 1, carry)
        return prof, rows

    def expected(self, cust: int, rows: list[dict]) -> dict:
        trades = [tax_calc.Trade(**{k: v for k, v in r.items() if k != "customer_id"}) for r in rows]
        methods = {}
        for r in rows:
            base = BROKER_METHOD[r["broker_code"]]
            methods[r["account_id"]] = "FIFO" if base == "CHOICE" else base
        res = tax_calc.calculate(tax_calc.CalcRequest(customer_id=cust, tax_year=TAX_YEAR, trades=trades, account_methods=methods))
        return {
            "tax_krw": res.tax_krw, "total_gain_krw": res.total_gain_krw, "taxable_krw": res.taxable_krw,
            "by_account": [{"account_id": a.account_id, "method": a.method, "gain_krw": a.gain_krw} for a in res.by_account],
            "transferred_lot_sales": [s.sell_trade_id for s in res.sales if s.has_transferred_lot],
            "needs_confirmation": sorted({r["account_id"] for r in rows if BROKER_METHOD[r["broker_code"]] == "CHOICE"}),
            "n_sales": len(res.sales),
        }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--customers", type=int, default=300)
    ap.add_argument("--out", default=str(ROOT / "data" / "synthetic"))
    args = ap.parse_args()
    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)

    g = Gen(args.seed)
    plan = [s for s, w in SCENARIOS for _ in range(w)]
    customers, trades, expected, errors = [], [], {}, []
    for i in range(args.customers):
        cust = 2001 + i
        scenario = plan[i % len(plan)]
        prof, rows = g.customer(cust, scenario)
        try:
            exp = g.expected(cust, rows)
        except tax_calc.CalcError as e:
            errors.append((cust, scenario, e.code, str(e)[:80]))
            continue
        customers.append(prof); trades += rows; expected[str(cust)] = exp

    (out / "customers.json").write_text(json.dumps(customers, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "trades.json").write_text(json.dumps(trades, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "expected.json").write_text(json.dumps({"seed": args.seed, "tax_year": TAX_YEAR, "customers": expected}, ensure_ascii=False, indent=1), encoding="utf-8")
    with (out / "fx_rate.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f); w.writerow(["rate_date", "usd_krw"])
        for d, v in sorted(g.fx.items()):
            w.writerow([d.isoformat(), v])

    sc = Counter(c["scenario"] for c in customers)
    print(f"seed={args.seed}  고객 {len(customers)}  거래 {len(trades)}  종목 {len(TICKERS)}  증권사 {len(BROKERS)}  → {out}/")
    print("시나리오:", dict(sc))
    ex = expected.values()
    print(f"tax_krw > 0: {sum(e['tax_krw'] > 0 for e in ex)}  손실(total_gain<0): {sum(e['total_gain_krw'] < 0 for e in ex)}  "
          f"매도 없음: {sum(e['n_sales'] == 0 for e in ex)}  이체 lot 매도: {sum(bool(e['transferred_lot_sales']) for e in ex)}  "
          f"needs_confirmation: {sum(bool(e['needs_confirmation']) for e in ex)}")
    print(f"계좌 2개 이상: {sum(len(c['accounts']) >= 2 for c in customers)}  2024 lot 이월: {sum(c['carry_2024_lots'] for c in customers)}")
    if errors:
        print(f"tax_calc 오류 {len(errors)}건 (제외됨):", errors[:5])
        return 1
    print("tax_calc 오류 0건 ✓  (모든 고객이 계산 가능)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
