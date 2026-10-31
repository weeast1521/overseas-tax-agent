"""실데이터 CSV 의 '모양'만 보여 준다 — 열 이름, 구분 값, 값의 형식. 값 자체는 가린다 (SCRUM-1).

    python scripts/peek_csv.py data/mine/<파일>.csv [--broker mirae|namuh]

출력은 붙여 넣어도 되도록 만든다:
  - 열 이름: 그대로 (머리글은 증권사 공통 양식이라 개인정보가 아니다)
  - 데이터 행 2개: 숫자 → #, 영문 → A/a, 한글 → 가 (날짜·금액 형식, 티커인지 종목명인지만 보인다)
  - '구분/종류' 열의 서로 다른 값: 그대로 (매수·매도·입고 같은 단어만)
  - --broker 를 주면 현재 매퍼로 읽어 보고 건수 또는 '없는 열' 오류만 보여 준다
계좌번호·종목·수량·금액은 출력에 남지 않는다. 파일은 읽기만 한다.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.importers.base import read_csv  # noqa: E402

_KIND_COLS = re.compile(r"구분|종류|유형|매매|거래명")


def mask(v: str) -> str:
    v = re.sub(r"\d", "#", str(v))
    v = re.sub(r"[A-Z]", "A", v)
    v = re.sub(r"[a-z]", "a", v)
    return re.sub(r"[가-힣]", "가", v)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--broker", choices=["mirae", "namuh"])
    a = ap.parse_args()
    rows = read_csv(a.path)
    if not rows:
        print("행 없음"); return
    cols = list(rows[0].keys())
    print(f"행 수: {len(rows)}  열 수: {len(cols)}")
    print("열 이름:", " | ".join(cols))
    for i, r in enumerate(rows[:2], 1):
        print(f"행{i} 모양:", " | ".join(mask(r[c]) for c in cols))
    for c in cols:
        if _KIND_COLS.search(c):
            vals = sorted({str(r[c]).strip() for r in rows})
            if len(vals) <= 15 and not any(re.search(r"\d{3,}", v) for v in vals):
                print(f"'{c}' 값 종류:", vals)
    if a.broker:
        from app.importers import mirae, namuh
        mod = {"mirae": mirae, "namuh": namuh}[a.broker]
        try:
            out = mod.load(a.path)
            sides = sorted({t.side for t in out})
            print(f"현재 매퍼로 읽기 성공: {len(out)}건, 구분 {sides}, 결제일 없는 건 {sum(1 for t in out if t.settle_date is None)}")
        except Exception as e:  # 어떤 열이 없는지만 — 값은 오류 메시지에 들어가지 않게 잘라서
            print(f"현재 매퍼로 읽기 실패: {type(e).__name__}: {mask(str(e))[:200]}")


if __name__ == "__main__":
    main()
