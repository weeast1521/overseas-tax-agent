"""F08-A 증권사 CSV → Trade 통합.

각 증권사 매퍼는 '컬럼 후보 목록'으로 동작한다. 증권사 앱이 내보내는 CSV의 정확한 헤더는
직접 내보내 보고 COLUMNS를 맞춘다. 헤더가 하나라도 안 맞으면 ImportError로 어떤 컬럼이
없는지 알려준다. 숫자는 쉼표·통화기호를 제거하고 Decimal로 파싱한다.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable

from app.tools.tax_calc import Trade


class ImportError_(Exception):
    pass


@dataclass
class RawTrade:
    """매퍼가 만드는 중간 형태. fx는 아직 없다."""
    broker_code: str
    account_id: str
    trade_id: str
    ticker: str
    side: str
    qty: Decimal
    price_usd: Decimal
    fee_usd: Decimal
    trade_date: date
    settle_date: date | None  # CSV에 없으면 None → settle.py가 채움


_NUM = re.compile(r"[^0-9.\-]")


def num(s: str | None, default: Decimal | None = None) -> Decimal:
    if s is None or str(s).strip() == "":
        if default is not None:
            return default
        raise ImportError_("빈 숫자 값")
    try:
        return Decimal(_NUM.sub("", str(s)))
    except InvalidOperation as e:
        raise ImportError_(f"숫자 파싱 실패: {s!r}") from e


def dt(s: str) -> date:
    s = str(s).strip()
    for fmt in ("%Y-%m-%d", "%Y.%m.%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            from datetime import datetime
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ImportError_(f"날짜 파싱 실패: {s!r}")


def pick(row: dict, candidates: Iterable[str], *, required=True) -> str | None:
    for c in candidates:
        for k in row:
            if k and k.strip().replace(" ", "") == c.replace(" ", ""):
                return row[k]
    if required:
        raise ImportError_(f"컬럼 없음: {list(candidates)} / 실제 헤더: {list(row.keys())}")
    return None


def read_csv(path: str | Path) -> list[dict]:
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "cp949", "euc-kr"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ImportError_("인코딩 판별 실패 (utf-8/cp949 아님)")
    return list(csv.DictReader(io.StringIO(text)))


def side_of(s: str) -> str:
    s = str(s).strip()
    if any(k in s for k in ("매수", "BUY", "buy", "Buy")):
        return "BUY"
    if any(k in s for k in ("매도", "SELL", "sell", "Sell")):
        return "SELL"
    raise ImportError_(f"매매구분 해석 실패: {s!r}")
