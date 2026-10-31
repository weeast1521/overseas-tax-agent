"""증권사 양도소득세 명세서 PDF → CSV (맥에서만). 값은 화면에 내보내지 않고 모양만 출력한다.

    pip install pdfplumber
    python scripts/pdf_to_csv.py data/mine/namu_trades_2025.pdf            # → data/mine/namu_trades_2025.csv
    python scripts/pdf_to_csv.py data/mine/namu_trades_2025.pdf --header 양도일자

- 페이지마다 표를 뽑아 '양도일자'(--header) 가 든 줄을 머리글로 잡고, 그 뒤 줄들을 이어 붙인다
- 머리글이 두 줄로 나뉜 서식(위 칸 병합)도 위·아래 칸을 합쳐 한 머리글로 만든다
- 페이지마다 반복되는 머리글·소계·합계 줄은 버린다
출력: 페이지 수, 머리글, 데이터 줄 수, 줄 모양 2개(숫자→#, 한글→가) — 붙여 넣어도 된다.
PDF·CSV 는 data/mine 에만 남는다 (git 제외).
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.peek_csv import mask  # noqa: E402


def clean(c) -> str:
    return " ".join(str(c or "").split())


def find_header(rows: list[list[str]], key: str) -> int | None:
    for i, r in enumerate(rows):
        if any(key in c.replace(" ", "") for c in r):
            return i
    return None


def merge_header(top: list[str], bottom: list[str] | None) -> list[str]:
    """두 줄 머리글: 아래 줄에 글자가 있고 날짜·숫자가 아니면 위 칸과 합친다."""
    if not bottom or any(re.match(r"^\d", c) for c in bottom if c):
        return top
    out, last = [], ""
    for t, b in zip(top, bottom):
        last = t or last
        out.append((last + b) if b else t)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--out")
    ap.add_argument("--header", default="양도일자")
    a = ap.parse_args()
    import pdfplumber

    header: list[str] | None = None
    data: list[list[str]] = []
    pages = 0
    with pdfplumber.open(a.pdf) as pdf:
        pages = len(pdf.pages)
        for page in pdf.pages:
            for tbl in page.extract_tables():
                rows = [[clean(c) for c in r] for r in tbl if r]
                h = find_header(rows, a.header)
                start = 0
                if h is not None:
                    two = rows[h + 1] if h + 1 < len(rows) else None
                    hdr = merge_header(rows[h], two)
                    start = h + (2 if hdr != rows[h] else 1)
                    header = header or hdr
                if header is None:
                    continue
                for r in rows[start:]:
                    if len(r) != len(header) or not any(r):
                        continue
                    if any(k in "".join(r) for k in ("소계", "합계", "총계", "Total")):
                        continue
                    data.append(r)
    if header is None:
        print(f"페이지 {pages} — '{a.header}' 머리글이 있는 표를 못 찾았습니다. 표 대신 글자로 된 PDF 일 수 있습니다.")
        with pdfplumber.open(a.pdf) as pdf:
            t = (pdf.pages[0].extract_text() or "").splitlines()
        print("첫 페이지 줄 모양(가림):")
        for line in t[:15]:
            print("  ", mask(line)[:120])
        sys.exit(2)
    out = Path(a.out or Path(a.pdf).with_suffix(".csv"))
    with out.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(data)
    print(f"페이지 {pages} · 데이터 줄 {len(data)} → {out}")
    print("머리글:", " | ".join(header))
    for r in data[:2]:
        print("줄 모양:", " | ".join(mask(c) for c in r))


if __name__ == "__main__":
    main()
