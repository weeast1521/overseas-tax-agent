"""결제일 보정(T+2 미국 영업일)과 결제일 환율 부착.

- 결제일이 CSV에 없으면 체결일 + 미국 영업일 2일. 주말과 data/holidays_us.csv의 휴장일을 건너뛴다.
- 환율은 data/fx_rate.csv (rate_date, usd_krw). 결제일이 한국 휴일이면 직전 영업일 환율.
  ★ 이 규칙은 국세청 2025년 귀속 안내로 확정 후 조정.
"""
from __future__ import annotations

import csv
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from app.importers.base import ImportError_, RawTrade
from app.tools.tax_calc import Trade

DATA = Path(__file__).resolve().parents[2] / "data"


def load_holidays(path: Path = DATA / "holidays_us.csv") -> set[date]:
    if not path.exists():
        return set()
    with path.open(encoding="utf-8") as f:
        return {date.fromisoformat(r["date"]) for r in csv.DictReader(f) if r.get("date")}


def load_fx(path: Path = DATA / "fx_rate.csv") -> dict[date, Decimal]:
    if not path.exists():
        raise ImportError_(f"환율 파일 없음: {path}. 서울외국환중개 매매기준율을 rate_date,usd_krw 형식으로 저장")
    with path.open(encoding="utf-8") as f:
        return {date.fromisoformat(r["rate_date"]): Decimal(r["usd_krw"]) for r in csv.DictReader(f)}


def us_settle(trade_date: date, holidays: set[date], days: int = 2) -> date:
    d = trade_date
    n = 0
    while n < days:
        d += timedelta(days=1)
        if d.weekday() < 5 and d not in holidays:
            n += 1
    return d


def fx_for(settle: date, fx: dict[date, Decimal], lookback: int = 7) -> Decimal:
    d = settle
    for _ in range(lookback):
        if d in fx:
            return fx[d]
        d -= timedelta(days=1)
    raise ImportError_(f"결제일 {settle} 전후 {lookback}일 환율 없음 (E-CALC-FX)")


def finalize(raws: list[RawTrade], fx: dict[date, Decimal] | None = None,
             holidays: set[date] | None = None) -> list[Trade]:
    fx = fx if fx is not None else load_fx()
    holidays = holidays if holidays is not None else load_holidays()
    seen: set[str] = set()
    out: list[Trade] = []
    for r in raws:
        if r.trade_id in seen:
            continue  # F08-4 중복 제거
        seen.add(r.trade_id)
        settle = r.settle_date or us_settle(r.trade_date, holidays)
        out.append(Trade(
            trade_id=r.trade_id, account_id=r.account_id, broker_code=r.broker_code, ticker=r.ticker, side=r.side,
            qty=r.qty, price_usd=r.price_usd, fee_usd=r.fee_usd,
            trade_date=r.trade_date, settle_date=settle, fx_settle=fx_for(settle, fx),
        ))
    return sorted(out, key=lambda t: (t.settle_date, t.trade_date, t.trade_id))
