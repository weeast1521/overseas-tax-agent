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
    """두 줄 머리글: 아래 줄에 글자가 있고 날짜·숫자가 아니면 위 칸과 합친다.
    위 줄이 표 제목 하나뿐(병합 칸: 값이 한 가지)이면 아래 줄만 쓴다."""
    if not bottom or any(re.match(r"^\d", c) for c in bottom if c):
        return top
    if len({c for c in top if c}) <= 1:
        return bottom
    out, last = [], ""
    for t, b in zip(top, bottom):
        last = t or last
        out.append((last + b) if b else t)
    return out


def name_of(c: str) -> str:
    """'(23)양도 일자' → '양도 일자', '양도가액 <(22)*(24)>' → '양도가액' (홈택스 서식 번호·계산식 제거)."""
    c = re.sub(r"<[^>]*>", "", c)
    c = re.sub(r"^\(\d+\)", "", c.strip())
    return c.strip()


def fit(row: list[str], header: list[str], keep: list[int]) -> list[str] | None:
    """병합 칸 때문에 생긴 빈 열을 떼고 머리글 이름 수에 맞춘다. 맞출 수 없으면 None."""
    n = len(keep)
    if len(row) == len(header):
        return [row[i] for i in keep]
    vals = [c for c in row if c]
    if len(row) == n:
        return row
    if n - 1 <= len(vals) <= n:  # 끝 열(비고)이 빈 경우
        return vals + [""] * (n - len(vals))
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--out")
    ap.add_argument("--header", default="양도일자")
    a = ap.parse_args()
    import pdfplumber

    from collections import Counter
    skipped: Counter = Counter()
    header: list[str] | None = None
    data: list[list[str]] = []
    pages = 0
    with pdfplumber.open(a.pdf) as pdf:
        pages = len(pdf.pages)
        for page in pdf.pages:
            page = page.dedupe_chars(tolerance=1)  # 굵은 글씨를 겹쳐 찍은 PDF: '양양도도' → '양도'
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
                keep = [i for i, c in enumerate(header) if c]
                for r in rows[start:]:
                    if not any(r) or any(k in "".join(r) for k in ("소계", "합계", "총계", "Total")):
                        skipped["소계·빈 줄"] += 1
                        continue
                    if find_header([r], a.header) is not None or r[keep[0]:keep[0] + 1] == [header[keep[0]]]:
                        skipped["반복 머리글"] += 1
                        continue
                    f = fit(r, header, keep)
                    if f is None:
                        skipped[f"열 수 {len(r)}(값 {sum(1 for c in r if c)})"] += 1
                        continue
                    data.append(f)
    if header is None:
        print(f"페이지 {pages} — '{a.header}' 머리글이 있는 표를 못 찾았습니다. 표 대신 글자로 된 PDF 일 수 있습니다.")
        with pdfplumber.open(a.pdf) as pdf:
            for n, pg in enumerate(pdf.pages, 1):
                pg = pg.dedupe_chars(tolerance=1)
                text = pg.extract_text() or ""
                hit = a.header in text.replace(" ", "")
                print(f"\n[{n}쪽] 표 {len(pg.extract_tables())}개 · '{a.header}' 글자 {'있음' if hit else '없음'}")
                if hit:
                    lines = text.splitlines()
                    i = next(k for k, l in enumerate(lines) if a.header in l.replace(' ', ''))
                    for l in lines[max(0, i - 2):i + 4]:  # 머리글 근처: 한글 머리글은 그대로, 값 줄은 가림
                        print("   ", l[:150] if not re.search(r"\d{2,}", l) else mask(l)[:150])
        sys.exit(2)
    names = [name_of(header[i]) for i in range(len(header)) if header[i]]
    out = Path(a.out or Path(a.pdf).with_suffix(".csv"))
    with out.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(names)
        w.writerows(data)
    print(f"페이지 {pages} · 데이터 줄 {len(data)} → {out}")
    print("머리글:", " | ".join(names))
    if skipped:
        print("버린 줄:", dict(skipped))
    for r in data[:2]:
        print("줄 모양:", " | ".join(mask(c) for c in r))


if __name__ == "__main__":
    main()
