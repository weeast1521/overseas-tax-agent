"""F05 테스트 — 계좌 단위 lot 매칭, 계좌별 방식, 이체 복원, 납세자 단위 통산."""
from datetime import date
from decimal import Decimal as D

import pytest

from app.tools.tax_calc import CalcRequest, CalcShortError, CalcTransferError, Trade, calculate

FX = D("1300")
MIRAE, NAMUH = ("MIRAE-001", "MIRAE"), ("NAMUH-001", "NAMUH")


def t(id, side, qty, price, settle, acct=MIRAE, ticker="TSLA", fee=0, fx=FX, trade=None, transfer_id=None):
    settle = date.fromisoformat(settle)
    return Trade(trade_id=id, account_id=acct[0], broker_code=acct[1], ticker=ticker, side=side, qty=D(qty),
                 price_usd=D(price), fee_usd=D(fee), trade_date=date.fromisoformat(trade) if trade else settle,
                 settle_date=settle, fx_settle=fx, transfer_id=transfer_id)


def run(trades, year=2025, methods=None):
    return calculate(CalcRequest(customer_id=1, tax_year=year, trades=trades, account_methods=methods or {}))


def sale(r, i=0):
    return r.sales[i]


# ── 계좌 단위 ─────────────────────────────────────────────────────────

def test_no_transfer_each_account_computes_alone():
    """미래에셋 1월 10@100, 나무 3월 10@200, 나무 5월 매도 10@180, 이체 없음 → 나무 lot만 → -200."""
    r = run([t("m1", "BUY", 10, 100, "2025-01-13", MIRAE),
             t("n1", "BUY", 10, 200, "2025-03-12", NAMUH),
             t("n2", "SELL", 10, 180, "2025-05-21", NAMUH)])
    s = sale(r)
    assert s.account_id == "NAMUH-001" and s.usd_gain == D("-200.00")
    assert s.gain_krw == -260_000
    assert [l.origin_account_id for l in s.lots_used] == ["NAMUH-001"]
    assert not s.has_transferred_lot
    assert len(r.by_account) == 1  # 미래에셋은 매도 없음


def test_transfer_carries_original_cost():
    """미래에셋 10@100 → 나무로 이체 → 나무에서 10@180 매도 → 원 취득가 100 적용 → +800."""
    r = run([t("m1", "BUY", 10, 100, "2025-01-13", MIRAE),
             t("m2", "TRANSFER_OUT", 10, 0, "2025-04-10", MIRAE, transfer_id="tr1"),
             t("n0", "TRANSFER_IN", 10, 0, "2025-04-10", NAMUH, transfer_id="tr1"),
             t("n2", "SELL", 10, 180, "2025-05-21", NAMUH)])
    s = sale(r)
    assert s.usd_gain == D("800.00")
    assert s.lots_used[0].origin_account_id == "MIRAE-001" and s.lots_used[0].transferred
    assert s.has_transferred_lot


def test_transfer_plus_local_lot_fifo_vs_avg():
    """이체 10@100 + 나무 매수 10@200, 나무에서 10@180 매도 → FIFO +800 / AVG +300."""
    trades = [t("m1", "BUY", 10, 100, "2025-01-13", MIRAE),
              t("m2", "TRANSFER_OUT", 10, 0, "2025-02-10", MIRAE, transfer_id="tr1"),
              t("n0", "TRANSFER_IN", 10, 0, "2025-02-10", NAMUH, transfer_id="tr1"),
              t("n1", "BUY", 10, 200, "2025-03-12", NAMUH),
              t("n2", "SELL", 10, 180, "2025-05-21", NAMUH)]
    fifo = run(trades, methods={"NAMUH-001": "FIFO"})
    avg = run(trades, methods={"NAMUH-001": "AVG"})
    assert sale(fifo).usd_gain == D("800.00")
    assert sale(avg).usd_gain == D("300.00")
    assert sale(avg).gain_krw == sale(fifo).avg_cost_gain_krw
    assert [l.qty for l in sale(avg).lots_used] == [D(5), D(5)]


def test_methods_differ_per_account_and_sum_at_person_level():
    """미래에셋 FIFO, 나무 AVG — 각자 방식으로 계산하고 사람 단위로 더한다."""
    trades = [t("m1", "BUY", 10, 100, "2025-01-13", MIRAE), t("m2", "BUY", 10, 200, "2025-03-12", MIRAE),
              t("m3", "SELL", 10, 180, "2025-05-21", MIRAE),
              t("n1", "BUY", 10, 100, "2025-01-13", NAMUH), t("n2", "BUY", 10, 200, "2025-03-12", NAMUH),
              t("n3", "SELL", 10, 180, "2025-05-21", NAMUH)]
    r = run(trades, methods={"MIRAE-001": "FIFO", "NAMUH-001": "AVG"})
    by = {a.account_id: a for a in r.by_account}
    assert by["MIRAE-001"].method == "FIFO" and by["MIRAE-001"].gain_krw == 1_040_000
    assert by["NAMUH-001"].method == "AVG" and by["NAMUH-001"].gain_krw == 390_000
    assert r.total_gain_krw == 1_430_000
    assert r.taxable_krw == 0  # 250만 공제


def test_transfer_in_without_out_raises():
    with pytest.raises(CalcTransferError):
        run([t("n0", "TRANSFER_IN", 10, 0, "2025-04-10", NAMUH, transfer_id="ghost"),
             t("n2", "SELL", 10, 180, "2025-05-21", NAMUH)])


def test_sell_more_than_account_holds_raises_short():
    """다른 계좌에 lot이 있어도 이 계좌엔 없으면 SHORT — 이체 누락 신호."""
    with pytest.raises(CalcShortError) as e:
        run([t("m1", "BUY", 10, 100, "2025-01-13", MIRAE),
             t("n2", "SELL", 10, 180, "2025-05-21", NAMUH)])
    assert e.value.account_id == "NAMUH-001" and e.value.short_qty == 10


# ── 기존 규칙 (계좌 1개 안에서) ────────────────────────────────────────

def test_basic_fifo():
    r = run([t("b1", "BUY", 10, 100, "2025-01-13"), t("b2", "BUY", 10, 200, "2025-03-12"),
             t("s1", "SELL", 10, 180, "2025-05-21")])
    assert sale(r).cost_krw == 1_300_000 and sale(r).gain_krw == 1_040_000
    assert sale(r).lots_used[0].buy_trade_id == "b1"


def test_partial_lot():
    r = run([t("b1", "BUY", 10, 100, "2025-01-13"), t("s1", "SELL", 4, 120, "2025-02-10"),
             t("s2", "SELL", 6, 130, "2025-03-10")])
    assert [s.lots_used[0].buy_trade_id for s in r.sales] == ["b1", "b1"]
    assert sum(s.cost_krw for s in r.sales) == 1_300_000


def test_fx_reversal():
    r = run([t("b1", "BUY", 10, 100, "2025-01-13", fx=D("1450")), t("s1", "SELL", 10, 105, "2025-05-21", fx=D("1300"))])
    assert sale(r).usd_gain == D("50.00") and sale(r).gain_krw < 0


def test_year_boundary_excluded():
    r = run([t("b1", "BUY", 10, 100, "2025-01-13"), t("s1", "SELL", 10, 180, "2026-01-02", trade="2025-12-30")])
    assert r.sales == [] and r.tax_krw == 0


def test_offset_across_accounts_and_tickers():
    r = run([t("b1", "BUY", 10, 100, "2025-01-13", MIRAE, "TSLA"), t("s1", "SELL", 10, 100 + D("3000000") / 10 / FX, "2025-05-21", MIRAE, "TSLA"),
             t("b2", "BUY", 10, 200, "2025-01-13", NAMUH, "LCID"), t("s2", "SELL", 10, 200 - D("2000000") / 10 / FX, "2025-05-21", NAMUH, "LCID")])
    assert r.total_gain_krw == 1_000_000 and r.taxable_krw == 0


def test_loss_no_deduction():
    r = run([t("b1", "BUY", 10, 100, "2025-01-13"), t("s1", "SELL", 10, 50, "2025-05-21")])
    assert r.deduction_krw == 0 and r.taxable_krw == 0


def test_fees_applied():
    r = run([t("b1", "BUY", 10, 100, "2025-01-13", fee=1), t("s1", "SELL", 10, 100, "2025-05-21", fee=1)])
    assert sale(r).cost_krw == 1_301_300 and sale(r).proceeds_krw == 1_298_700


def test_tax_rate_and_truncation():
    r = run([t("b1", "BUY", 10, 100, "2025-01-13"), t("s1", "SELL", 10, 100 + D("5000001") / 10 / FX, "2025-05-21")])
    assert r.taxable_krw == 2_500_001 and r.tax_krw == 550_000


def test_avg_total_equals_fifo_when_fully_sold():
    trades = [t("b1", "BUY", 10, 100, "2025-01-13"), t("b2", "BUY", 10, 200, "2025-03-12"),
              t("s1", "SELL", 10, 180, "2025-05-21"), t("s2", "SELL", 10, 220, "2025-07-16")]
    assert run(trades).total_gain_krw == run(trades, methods={"MIRAE-001": "AVG"}).total_gain_krw
