"""나무증권(NH투자증권 나무) 해외주식 체결내역 CSV 매퍼.

★ 확인 필요: 나무 앱 '거래내역 조회 → 해외주식' 또는 NH 홈페이지 내보내기 헤더를 실제로 열어서
COLUMNS 후보를 맞출 것. NH 계열은 '거래금액(외화)', '정산금액' 같은 컬럼이 따로 있는 경우가 많다.
"""
from __future__ import annotations

from app.importers.base import RawTrade, dt, num, pick, read_csv, side_of

BROKER = "NAMUH"
COLUMNS = {
    "trade_date": ["거래일자", "거래일", "체결일자", "매매일"],
    "settle_date": ["결제일자", "결제일"],
    "ticker": ["종목코드", "심볼", "티커", "종목"],
    "name": ["종목명"],
    "side": ["거래구분", "매매구분", "거래명", "적요"],
    "qty": ["거래수량", "수량", "체결수량"],
    "price": ["거래단가", "단가", "체결단가", "체결가격"],
    "fee": ["수수료", "제세금", "수수료(외화)"],
    "account": ["계좌번호", "계좌", "위탁계좌번호"],
    "seq": ["일련번호", "거래번호", "순번"],
}


def load(path: str) -> list[RawTrade]:
    out = []
    for i, row in enumerate(read_csv(path)):
        side = side_of(pick(row, COLUMNS["side"]))
        trade_date = dt(pick(row, COLUMNS["trade_date"]))
        settle_raw = pick(row, COLUMNS["settle_date"], required=False)
        seq = pick(row, COLUMNS["seq"], required=False) or str(i)
        acct_raw = pick(row, COLUMNS["account"], required=False)
        account_id = f"{BROKER}-{str(acct_raw).strip()[-4:]}" if acct_raw else f"{BROKER}-001"
        out.append(RawTrade(
            broker_code=BROKER,
            account_id=account_id,
            trade_id=f"{BROKER}-{trade_date:%Y%m%d}-{seq}",
            ticker=str(pick(row, COLUMNS["ticker"])).strip().upper(),
            side=side,
            qty=num(pick(row, COLUMNS["qty"])),
            price_usd=num(pick(row, COLUMNS["price"])),
            fee_usd=num(pick(row, COLUMNS["fee"], required=False), default=num("0")),
            trade_date=trade_date,
            settle_date=dt(settle_raw) if settle_raw else None,
        ))
    return out
