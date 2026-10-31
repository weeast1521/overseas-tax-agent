"""증권사 양도소득세 계산명세서 파서 — 홈택스 '주식등 양도소득금액 계산명세서' 서식 (교차검증의 정답지 쪽).

증권사가 신고대행 때 만드는 lot 단위 명세서. 한 줄 = "양도일자에 판 n주를 취득일자에 산 lot 과 맞춘 것".
금액은 이미 원화(결제일 환율 적용 후)이고, 국세청 코드 열(세율구분 61 = 국외주식, 국외자산국가코드 등)이 붙는다.
미래에셋 카이로스 내보내기로 확인한 머리글을 기준으로, 다른 증권사도 같은 서식일 것을 가정해 후보 이름을 둔다.

이 파일은 계산기의 입력이 아니다(달러 단가·환율이 없다). 계산기 결과와 건별로 대조하는 정답지다.
실데이터 — data/mine 에만 두고, 출력은 집계만 한다 (scripts/check_statement.py).
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from app.importers.base import ImportError_, dt, num, pick, read_csv

COLUMNS = {
    "name": ["주식 종목명", "종목명"],
    "isin": ["국제증권식별번호(ISIN코드)", "국제증권식별번호", "ISIN코드", "ISIN", "표준종목번호"],
    "ticker": ["종목코드"],
    "qty": ["취득유형별양도주식 수", "취득유형별양도주식수", "양도주식 수", "양도주식수"],
    "sell_date": ["양도일자"],
    "sell_price": ["주당양도가액"],
    "proceeds": ["양도가액", "양도가액(원)"],
    "acq_date": ["취득일자"],
    "acq_price": ["주당취득가액"],
    "cost": ["취득가액", "취득가액(원)"],
    "expenses": ["필요경비", "필요경비(원)"],
    "country": ["국외자산국가코드"],
    # 확장판(카이로스 '해외주식 양도소득 명세' 내보내기)에만 있는 열: 달러 단가와 환율 — 원화를 줄마다 다시 계산할 수 있다
    "sell_usd": ["매도단가"],
    "buy_usd": ["매수단가"],
    "fx_sell": ["매도일환율"],
    "fx_buy": ["매수일환율"],
}
# 계좌번호 열은 읽지 않는다 (계좌는 MIRAE-001 같은 내부 식별자로만 다룬다)


@dataclass(frozen=True)
class StatementLot:
    name: str
    isin: str
    qty: Decimal
    sell_date: date
    sell_price_krw: Decimal
    proceeds_krw: Decimal
    acq_date: date
    acq_price_krw: Decimal
    cost_krw: Decimal
    expenses_krw: Decimal
    ticker: str = ""
    sell_usd: Decimal | None = None
    buy_usd: Decimal | None = None
    fx_sell: Decimal | None = None
    fx_buy: Decimal | None = None

    @property
    def gain_krw(self) -> Decimal:
        return self.proceeds_krw - self.cost_krw - self.expenses_krw


def _key(row: dict) -> dict:
    """머리글의 줄바꿈·공백 차이를 흡수 (카이로스는 '취득유형별\\r\\n양도주식 수' 처럼 줄바꿈을 넣는다)."""
    return {" ".join(str(k).split()).replace(" ", "") if k else k: v for k, v in row.items()}


def _pick(row: dict, field: str, required: bool = True):
    return pick(row, [c.replace(" ", "") for c in COLUMNS[field]], required=required)


def _opt(row: dict, field: str) -> Decimal | None:
    v = _pick(row, field, required=False)
    return None if v is None or str(v).strip() == "" else num(v)


def load(path: str) -> list[StatementLot]:
    out: list[StatementLot] = []
    for raw in read_csv(path):
        row = _key(raw)
        sell = _pick(row, "sell_date", required=False)
        if not sell or not str(sell).strip()[:1].isdigit():  # 소계·빈 줄·두 번째 머리글 줄
            continue
        out.append(StatementLot(
            name=str(_pick(row, "name")).strip(),
            isin=str(_pick(row, "isin", required=False) or "").strip().upper(),
            qty=num(_pick(row, "qty")),
            sell_date=dt(sell),
            sell_price_krw=num(_pick(row, "sell_price", required=False), default=Decimal(0)),
            proceeds_krw=num(_pick(row, "proceeds")),
            acq_date=dt(_pick(row, "acq_date")),
            acq_price_krw=num(_pick(row, "acq_price", required=False), default=Decimal(0)),
            cost_krw=num(_pick(row, "cost")),
            expenses_krw=num(_pick(row, "expenses"), default=Decimal(0)),
            ticker=str(_pick(row, "ticker", required=False) or "").strip().upper(),
            sell_usd=_opt(row, "sell_usd"), buy_usd=_opt(row, "buy_usd"),
            fx_sell=_opt(row, "fx_sell"), fx_buy=_opt(row, "fx_buy"),
        ))
    if not out:
        raise ImportError_("명세서 행 없음 — 양도일자 열이 있는 계산명세서인지 확인")
    return out


def totals(lots: list[StatementLot]) -> dict:
    p = sum((l.proceeds_krw for l in lots), Decimal(0))
    c = sum((l.cost_krw for l in lots), Decimal(0))
    e = sum((l.expenses_krw for l in lots), Decimal(0))
    return {"rows": len(lots), "securities": len({l.isin or l.name for l in lots}), "sales": len({(l.isin or l.name, l.sell_date) for l in lots}),
            "proceeds_krw": int(p), "cost_krw": int(c), "expenses_krw": int(e), "gain_krw": int(p - c - e)}


def method_signature(lots: list[StatementLot]) -> dict:
    """증권사가 실제로 쓴 산정 방식을 명세서 모양에서 추정한다 (값은 내보내지 않고 건수만).

    - 한 번의 매도(같은 종목·양도일자)가 서로 다른 주당취득가액의 여러 lot 으로 쪼개져 있으면 → lot 단위(선입선출 계열)
    - 한 매도가 늘 한 줄이고 주당취득가액이 하나면 → 이동평균 계열(또는 그 종목은 매수가 한 번뿐)
    - 같은 종목의 매도들이 시간순으로 갈 때 쓰인 취득일자가 거꾸로 가지 않으면 → 선입선출과 모순 없음
    """
    by_sale: dict[tuple, list[StatementLot]] = defaultdict(list)
    for l in lots:
        by_sale[(l.isin or l.name, l.sell_date)].append(l)
    split = sum(1 for g in by_sale.values() if len({(l.acq_date, l.acq_price_krw, l.buy_usd) for l in g}) > 1)
    by_sec: dict[str, list[StatementLot]] = defaultdict(list)
    for l in lots:
        by_sec[l.isin or l.name].append(l)
    fifo_ok = fifo_bad = 0
    for sec_lots in by_sec.values():
        sales = sorted({l.sell_date for l in sec_lots})
        if len(sales) < 2:
            continue
        prev_max = None
        bad = False
        for s in sales:
            acq = [l.acq_date for l in sec_lots if l.sell_date == s]
            if prev_max is not None and min(acq) < prev_max:
                bad = True
            prev_max = max(acq)
        fifo_bad += bad
        fifo_ok += not bad
    acq_after_sell = sum(1 for l in lots if l.acq_date > l.sell_date)
    if split and not fifo_bad:
        guess = "FIFO"
    elif not split and fifo_bad:
        guess = "AVG"
    elif not split and not fifo_bad:
        guess = "FIFO 또는 AVG (구분할 수 있는 매도가 없음)"
    else:
        guess = "혼합 — 계좌·종목별로 다를 수 있음"
    return {"sales_split_into_lots": split, "sales_total": len(by_sale), "securities_fifo_consistent": fifo_ok,
            "securities_fifo_violated": fifo_bad, "acq_after_sell_rows": acq_after_sell, "guess": guess}


# ── 확장판 전용: 달러×환율 재계산과 계산기 재생 ────────────────────────────────────────

def has_usd(lots: list[StatementLot]) -> bool:
    return all(l.sell_usd is not None and l.buy_usd is not None and l.fx_sell and l.fx_buy for l in lots)


def fx_recompute(lots: list[StatementLot], tol: int = 1) -> dict:
    """줄마다 수량×달러단가×환율 을 원 단위로 다시 계산해 명세서 원화와 비교한다 (건수·합계 차이만).

    증권사가 '단가×환율 후 곱하기'인지 '금액 후 환율'인지, 반올림/절사인지에 따라 ±1원이 나므로 tol 로 흡수한다.
    """
    from decimal import ROUND_DOWN, ROUND_HALF_UP
    out = {"rows": len(lots)}
    for side, usd, fx, krw in (("sell", "sell_usd", "fx_sell", "proceeds_krw"), ("buy", "buy_usd", "fx_buy", "cost_krw")):
        best = None
        for rule, rnd in (("반올림", ROUND_HALF_UP), ("절사", ROUND_DOWN)):
            ok = 0
            diff = Decimal(0)
            for l in lots:
                calc = (l.qty * getattr(l, usd) * getattr(l, fx)).quantize(Decimal(1), rounding=rnd)
                d = calc - getattr(l, krw)
                diff += d
                ok += abs(d) <= tol
            if best is None or ok > best[1]:
                best = (rule, ok, int(diff))
        out[side] = {"rule": best[0], "match": best[1], "sum_diff_krw": best[2]}
    return out


def to_trades(lots: list[StatementLot], account_id: str = "MIRAE-001", broker: str = "MIRAE"):
    """명세서 lot 을 매수·매도 거래로 되돌린다 → 우리 계산기(tax_calc)에 그대로 넣어 같은 답이 나오는지 본다.

    - 매수: (종목, 취득일자, 달러단가, 환율) 이 같은 줄을 한 건으로 합친다 (수량 합)
    - 매도: (종목, 양도일자, 달러단가) 를 한 건으로 합친다. 필요경비(원)는 매도 수수료로 넣되 매도환율로 달러 환산
    - 체결일=결제일=명세서 날짜 (명세서의 양도·취득일자는 이미 결제일 기준)
    이 재생은 '증권사가 맞춘 lot 을 우리 FIFO 가 똑같이 고르는가'와 '원화 환산식이 같은가'를 보는 것이다.
    명세서에 안 나오는 매수(아직 안 판 주식)는 FIFO 에서 늘 뒤쪽이라 빠져도 매칭이 바뀌지 않는다.
    """
    from app.tools.tax_calc import Trade
    buys: dict[tuple, Decimal] = defaultdict(Decimal)
    sells: dict[tuple, list] = {}
    for l in lots:
        sec = l.ticker or l.isin or l.name
        buys[(sec, l.acq_date, l.buy_usd, l.fx_buy)] += l.qty
        k = (sec, l.sell_date, l.sell_usd, l.fx_sell)
        q, fee = sells.get(k, (Decimal(0), Decimal(0)))
        sells[k] = (q + l.qty, fee + l.expenses_krw)
    out = []
    for i, ((sec, d, px, fx), q) in enumerate(sorted(buys.items(), key=lambda x: (x[0][0], x[0][1]))):
        out.append(Trade(trade_id=f"B{i:04d}", account_id=account_id, broker_code=broker, ticker=sec, side="BUY",
                         qty=q, price_usd=px, trade_date=d, settle_date=d, fx_settle=fx))
    for i, ((sec, d, px, fx), (q, fee_krw)) in enumerate(sorted(sells.items(), key=lambda x: (x[0][0], x[0][1]))):
        out.append(Trade(trade_id=f"S{i:04d}", account_id=account_id, broker_code=broker, ticker=sec, side="SELL",
                         qty=q, price_usd=px, fee_usd=fee_krw / fx, trade_date=d, settle_date=d, fx_settle=fx))
    return out
