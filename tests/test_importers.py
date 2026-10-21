"""F08-A 매퍼·결제일 보정·환율 부착 테스트."""
from datetime import date
from decimal import Decimal as D
from pathlib import Path

import pytest

from app.importers import mirae, namuh
from app.importers.base import ImportError_
from app.importers.settle import finalize, fx_for, us_settle


def test_us_settle_skips_weekend_and_holiday():
    hol = {date(2025, 7, 4)}
    assert us_settle(date(2025, 7, 2), hol) == date(2025, 7, 7)  # 목 → 금(휴장) → 월, 화
    assert us_settle(date(2025, 5, 19), set()) == date(2025, 5, 21)


def test_fx_lookback_to_previous_business_day():
    fx = {date(2025, 5, 20): D("1390")}
    assert fx_for(date(2025, 5, 21), fx) == D("1390")


def test_mirae_mapper_and_finalize(tmp_path: Path):
    p = tmp_path / "m.csv"
    p.write_text("거래일,종목코드,종목명,거래구분,수량,단가,수수료,거래번호\n"
                 "2025.05.19,TSLA,테슬라,해외주식매도,\"1,000\",180.5,0.25,77\n", encoding="utf-8-sig")
    raws = mirae.load(str(p))
    assert raws[0].side == "SELL" and raws[0].qty == D("1000") and raws[0].settle_date is None
    trades = finalize(raws, fx={date(2025, 5, 21): D("1386.4")}, holidays=set())
    assert trades[0].settle_date == date(2025, 5, 21)
    assert trades[0].fx_settle == D("1386.4")
    assert trades[0].trade_id == "MIRAE-20250519-77"
    assert trades[0].account_id == "MIRAE-001"


def test_namuh_mapper_cp949_and_missing_column(tmp_path: Path):
    p = tmp_path / "n.csv"
    p.write_bytes("거래일자,종목코드,거래구분,거래수량,거래단가\n2025-03-10,NVDA,매수,20,120\n".encode("cp949"))
    raws = namuh.load(str(p))
    assert raws[0].broker_code == "NAMUH" and raws[0].fee_usd == 0
    bad = tmp_path / "bad.csv"
    bad.write_text("날짜,코드\n2025-03-10,NVDA\n", encoding="utf-8")
    with pytest.raises(ImportError_):
        namuh.load(str(bad))


def test_duplicates_removed():
    raws = mirae.load.__globals__["RawTrade"]
    r = raws("MIRAE", "MIRAE-001", "MIRAE-20250519-1", "TSLA", "BUY", D(1), D(1), D(0), date(2025, 5, 19), None)
    out = finalize([r, r], fx={date(2025, 5, 21): D("1")}, holidays=set())
    assert len(out) == 1
