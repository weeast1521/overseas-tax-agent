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
    "isin": ["국제증권식별번호(ISIN코드)", "국제증권식별번호", "ISIN코드", "ISIN"],
    "qty": ["취득유형별양도주식 수", "취득유형별양도주식수", "양도주식 수", "양도주식수"],
    "sell_date": ["양도일자"],
    "sell_price": ["주당양도가액"],
    "proceeds": ["양도가액"],
    "acq_date": ["취득일자"],
    "acq_price": ["주당취득가액"],
    "cost": ["취득가액"],
    "expenses": ["필요경비"],
    "country": ["국외자산국가코드"],
}


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

    @property
    def gain_krw(self) -> Decimal:
        return self.proceeds_krw - self.cost_krw - self.expenses_krw


def _key(row: dict) -> dict:
    """머리글의 줄바꿈·공백 차이를 흡수 (카이로스는 '취득유형별\\r\\n양도주식 수' 처럼 줄바꿈을 넣는다)."""
    return {" ".join(str(k).split()).replace(" ", "") if k else k: v for k, v in row.items()}


def _pick(row: dict, field: str, required: bool = True):
    return pick(row, [c.replace(" ", "") for c in COLUMNS[field]], required=required)


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
            sell_price_krw=num(_pick(row, "sell_price"), default=Decimal(0)),
            proceeds_krw=num(_pick(row, "proceeds")),
            acq_date=dt(_pick(row, "acq_date")),
            acq_price_krw=num(_pick(row, "acq_price"), default=Decimal(0)),
            cost_krw=num(_pick(row, "cost")),
            expenses_krw=num(_pick(row, "expenses"), default=Decimal(0)),
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
    split = sum(1 for g in by_sale.values() if len({l.acq_price_krw for l in g}) > 1)
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
