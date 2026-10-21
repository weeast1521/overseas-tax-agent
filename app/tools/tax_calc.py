"""F05 세액 계산 툴 — 결정론적. LLM은 이 모듈의 출력을 바꾸지 못한다.

계산 단위 (2025-10-20 수정, 미래에셋 신고대행 안내·국세청 서면-2022-국제세원-0764 기준):
- 취득가액 산정(lot 매칭)은 **계좌 단위**다. 큐 키는 (account_id, ticker).
  각 증권사 계산내역서가 자기 계좌의 매수 이력으로 종목별 양도가액·취득가액을 내고,
  합산 신고는 그 계산내역서들의 숫자를 더하는 것이다. 타사 lot을 섞어 다시 매칭하지 않는다.
- 산정 방식(FIFO/AVG)도 계좌 단위다. 증권사마다 다르고, 그대로 합산된다.
- 계좌 간 이체(대체입고)된 주식은 원래 취득가액·취득일을 그대로 들고 옮긴다 (소득세법 §118의4 ① 1호).
  받은 증권사가 이체일 종가나 평균단가로 찍은 취득가액은 틀린 값이며, 이 툴은 원 lot으로 복원한다.
- 연간 통산·기본공제·세율은 **납세자 단위**다. 전 계좌 종목별 손익 합 → 2,500,000원 → 22%.

기타:
- 양도·취득가액 모두 결제일(settle_date) 매매기준율로 환산.
- 과세 대상은 settle_date가 과세연도 안인 SELL.
- 매도 수량이 그 계좌 큐 잔량보다 크면 CalcShortError (이체 누락 또는 매수 누락 신호).

세율·공제액·환율 기준일은 2025년 귀속 국세청 안내로 확정한다. 상수는 확인 전 값이다.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_validator

BASIC_DEDUCTION_KRW = Decimal("2500000")
TAX_RATE = Decimal("0.22")

Method = Literal["FIFO", "AVG"]
Side = Literal["BUY", "SELL", "TRANSFER_OUT", "TRANSFER_IN"]


class CalcError(Exception):
    code = "E-CALC"


class CalcShortError(CalcError):
    """매도(또는 이체 출고) 수량 > 그 계좌의 보유 lot. 이전 매수·이체 입고 누락 가능성."""
    code = "E-CALC-SHORT"

    def __init__(self, account_id: str, ticker: str, short_qty: Decimal):
        super().__init__(f"{account_id}/{ticker}: 수량이 보유 lot보다 {short_qty}주 많음")
        self.account_id = account_id
        self.ticker = ticker
        self.short_qty = short_qty


class CalcTransferError(CalcError):
    """TRANSFER_IN에 짝이 되는 TRANSFER_OUT이 없음."""
    code = "E-CALC-TRANSFER"


class Trade(BaseModel):
    trade_id: str
    account_id: str = Field(description="계좌 식별자. lot 매칭 단위")
    broker_code: str
    ticker: str
    side: Side
    qty: Decimal = Field(gt=0)
    price_usd: Decimal = Field(ge=0, default=Decimal("0"), description="TRANSFER는 0 (받은 쪽이 찍은 단가는 쓰지 않는다)")
    fee_usd: Decimal = Field(ge=0, default=Decimal("0"))
    trade_date: date
    settle_date: date
    fx_settle: Decimal = Field(gt=0, default=Decimal("1"), description="settle_date 매매기준율. TRANSFER는 미사용")
    transfer_id: str | None = Field(default=None, description="TRANSFER_OUT/IN 짝 맞추기용. 같은 값이 한 쌍")

    @field_validator("settle_date")
    @classmethod
    def _settle_after_trade(cls, v: date, info):
        td = info.data.get("trade_date")
        if td and v < td:
            raise ValueError("settle_date must be >= trade_date")
        return v


class CalcRequest(BaseModel):
    customer_id: int
    tax_year: int
    trades: list[Trade]
    account_methods: dict[str, Method] = Field(
        default_factory=dict,
        description="계좌별 산정 방식. 없는 계좌는 default_method. 증권사마다 다르며 그대로 합산된다")
    default_method: Method = "FIFO"


class LotUsed(BaseModel):
    buy_trade_id: str
    origin_account_id: str = Field(description="lot이 처음 매수된 계좌. 이체됐으면 매도 계좌와 다르다")
    origin_broker_code: str
    qty: Decimal
    cost_krw: int
    unit_price_usd: Decimal
    fx_buy: Decimal
    buy_date: date
    transferred: bool = False


class SaleResult(BaseModel):
    """매도 1건 단위 결과. 증권사 계산내역서의 한 행에 대응."""
    sell_trade_id: str
    account_id: str
    broker_code: str
    ticker: str
    method: Method
    sold_qty: Decimal
    settle_date: date
    proceeds_krw: int
    cost_krw: int
    gain_krw: int
    avg_cost_gain_krw: int = Field(description="참고: 매도 직전 계좌 내 이동평균 기준 차익")
    usd_gain: Decimal
    lots_used: list[LotUsed]
    has_transferred_lot: bool = Field(description="이체 입고된 lot이 섞여 있으면 True → 받은 증권사 계산서가 틀릴 수 있음")


class AccountResult(BaseModel):
    """계좌 1개 = 증권사 계산내역서 1장."""
    account_id: str
    broker_code: str
    method: Method
    sales: list[SaleResult]
    gain_krw: int


class CalcResult(BaseModel):
    customer_id: int
    tax_year: int
    by_account: list[AccountResult]
    total_gain_krw: int
    deduction_krw: int
    taxable_krw: int
    tax_krw: int
    fx_note: str = "양도·취득가액 모두 결제일 매매기준율로 환산"

    @property
    def sales(self) -> list[SaleResult]:
        return [s for a in self.by_account for s in a.sales]


@dataclass
class _Lot:
    buy_trade_id: str
    origin_account_id: str
    origin_broker_code: str
    qty: Decimal
    cost_krw: Decimal
    unit_price_usd: Decimal
    fx_buy: Decimal
    buy_date: date
    transferred: bool = False


@dataclass
class _Used:
    lot: _Lot
    qty: Decimal
    cost_krw: Decimal


def _won(d: Decimal) -> int:
    return int(d.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def calculate(req: CalcRequest) -> CalcResult:
    queues: dict[tuple[str, str], deque[_Lot]] = defaultdict(deque)
    in_transit: dict[str, list[_Lot]] = {}  # transfer_id → 출고된 lot 조각들
    acct_meta: dict[str, str] = {}
    sales_by_acct: dict[str, list[SaleResult]] = defaultdict(list)

    def method_of(acct: str) -> Method:
        return req.account_methods.get(acct, req.default_method)

    order = {"BUY": 0, "TRANSFER_OUT": 1, "TRANSFER_IN": 2, "SELL": 3}
    for t in sorted(req.trades, key=lambda x: (x.settle_date, order[x.side], x.trade_date, x.trade_id)):
        acct_meta.setdefault(t.account_id, t.broker_code)
        q = queues[(t.account_id, t.ticker)]

        if t.side == "BUY":
            cost = (t.price_usd * t.qty + t.fee_usd) * t.fx_settle
            q.append(_Lot(t.trade_id, t.account_id, t.broker_code, t.qty, cost, t.price_usd, t.fx_settle, t.settle_date))
            continue

        if t.side == "TRANSFER_OUT":
            if not t.transfer_id:
                raise CalcTransferError(f"{t.trade_id}: transfer_id 없음")
            used = _consume(q, t.account_id, t.ticker, t.qty, method_of(t.account_id))
            in_transit[t.transfer_id] = [
                _Lot(u.lot.buy_trade_id, u.lot.origin_account_id, u.lot.origin_broker_code, u.qty, u.cost_krw,
                     u.lot.unit_price_usd, u.lot.fx_buy, u.lot.buy_date, transferred=True) for u in used]
            continue

        if t.side == "TRANSFER_IN":
            lots = in_transit.pop(t.transfer_id or "", None)
            if lots is None:
                raise CalcTransferError(f"{t.trade_id}: 짝이 되는 TRANSFER_OUT 없음 (transfer_id={t.transfer_id})")
            moved = sum((l.qty for l in lots), Decimal("0"))
            if moved != t.qty:
                raise CalcTransferError(f"{t.trade_id}: 출고 {moved}주 ≠ 입고 {t.qty}주")
            # 원 lot을 그대로 받은 계좌 큐에 넣는다. 취득일 순서 유지.
            merged = sorted(list(q) + lots, key=lambda l: l.buy_date)
            q.clear()
            q.extend(merged)
            continue

        # SELL
        m = method_of(t.account_id)
        total_cost_before = sum((l.cost_krw for l in q), Decimal("0"))
        total_qty_before = sum((l.qty for l in q), Decimal("0"))
        used = _consume(q, t.account_id, t.ticker, t.qty, m)
        if t.settle_date.year != req.tax_year:
            continue  # 큐에서는 뺐지만 과세연도 밖

        cost = sum((u.cost_krw for u in used), Decimal("0"))
        proceeds = (t.price_usd * t.qty - t.fee_usd) * t.fx_settle
        avg_cost = total_cost_before / total_qty_before * t.qty if total_qty_before else Decimal("0")
        usd_cost = sum((u.lot.unit_price_usd * u.qty for u in used), Decimal("0"))

        sales_by_acct[t.account_id].append(SaleResult(
            sell_trade_id=t.trade_id, account_id=t.account_id, broker_code=t.broker_code, ticker=t.ticker,
            method=m, sold_qty=t.qty, settle_date=t.settle_date,
            proceeds_krw=_won(proceeds), cost_krw=_won(cost), gain_krw=_won(proceeds - cost),
            avg_cost_gain_krw=_won(proceeds - avg_cost),
            usd_gain=(t.price_usd * t.qty - usd_cost).quantize(Decimal("0.01")),
            lots_used=[LotUsed(buy_trade_id=u.lot.buy_trade_id, origin_account_id=u.lot.origin_account_id,
                               origin_broker_code=u.lot.origin_broker_code, qty=u.qty, cost_krw=_won(u.cost_krw),
                               unit_price_usd=u.lot.unit_price_usd, fx_buy=u.lot.fx_buy, buy_date=u.lot.buy_date,
                               transferred=u.lot.transferred) for u in used],
            has_transferred_lot=any(u.lot.transferred for u in used),
        ))

    by_account = [
        AccountResult(account_id=a, broker_code=acct_meta[a], method=method_of(a), sales=s,
                      gain_krw=sum(x.gain_krw for x in s))
        for a, s in sorted(sales_by_acct.items())
    ]
    total = sum(a.gain_krw for a in by_account)
    deduction = min(int(BASIC_DEDUCTION_KRW), total) if total > 0 else 0
    taxable = max(0, total - deduction)
    tax = int((Decimal(taxable) * TAX_RATE).quantize(Decimal("1"), rounding=ROUND_DOWN))
    return CalcResult(customer_id=req.customer_id, tax_year=req.tax_year, by_account=by_account,
                      total_gain_krw=total, deduction_krw=deduction, taxable_krw=taxable, tax_krw=tax)


def _consume(q: deque[_Lot], account_id: str, ticker: str, qty: Decimal, method: Method) -> list[_Used]:
    total_qty = sum((l.qty for l in q), Decimal("0"))
    if qty > total_qty:
        raise CalcShortError(account_id, ticker, qty - total_qty)
    used: list[_Used] = []
    if method == "AVG":
        ratio = qty / total_qty
        for lot in list(q):
            take, portion = lot.qty * ratio, lot.cost_krw * ratio
            used.append(_Used(lot, take, portion))
            lot.qty -= take
            lot.cost_krw -= portion
        while q and q[0].qty == 0:
            q.popleft()
        return used
    need = qty
    while need > 0:
        lot = q[0]
        take = min(need, lot.qty)
        portion = lot.cost_krw * (take / lot.qty)
        used.append(_Used(lot, take, portion))
        lot.cost_krw -= portion
        lot.qty -= take
        need -= take
        if lot.qty == 0:
            q.popleft()
    return used
