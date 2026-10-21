"""미래에셋증권 해외주식 체결내역 CSV 매퍼.

★ 확인 필요: 미래에셋 M-STOCK / 홈페이지의 '해외주식 거래내역' 내보내기 헤더를 실제로 열어서
COLUMNS 후보를 맞출 것. 아래는 흔히 쓰이는 표기들을 후보로 넣어 둔 것이다.
결제일 컬럼이 없으면 None으로 두고 settle.py가 T+2 영업일 규칙으로 채운다.
계좌번호 컬럼이 없으면 계좌 1개로 간주한다 (BROKER-001). 계좌가 여럿이면 CSV를 계좌별로 내보내거나 컬럼을 추가.
"""
from __future__ import annotations

from app.importers.base import RawTrade, dt, num, pick, read_csv, side_of

BROKER = "MIRAE"
COLUMNS = {
    "trade_date": ["거래일", "거래일자", "체결일", "주문일자"],
    "settle_date": ["결제일", "결제일자"],
    "ticker": ["종목코드", "티커", "심볼", "종목"],
    "name": ["종목명"],
    "side": ["거래구분", "매매구분", "거래종류"],
    "qty": ["수량", "체결수량", "거래수량"],
    "price": ["단가", "체결단가", "거래단가", "체결가"],
    "fee": ["수수료", "제비용", "수수료(외화)"],
    "account": ["계좌번호", "계좌", "위탁계좌번호"],
    "seq": ["거래번호", "주문번호", "번호"],
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
