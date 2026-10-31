"""증권사 계산명세서 파서 (교차검증 정답지). 픽스처는 카이로스 머리글 그대로, 값은 지어낸 것."""
from pathlib import Path

from app.importers import statement

FIX = Path(__file__).parent / "fixtures_statement.csv"


def test_parses_kairos_headers_with_linebreaks():
    lots = statement.load(str(FIX))
    assert len(lots) == 4 and lots[0].isin == "US0000000001" and lots[0].qty == 10
    assert lots[0].gain_krw == 1_500_000 - 1_400_000 - 3_700


def test_totals_follow_statement_identity():
    t = statement.totals(statement.load(str(FIX)))
    assert t == {"rows": 4, "securities": 2, "sales": 3, "proceeds_krw": 3_050_000, "cost_krw": 3_030_000,
                 "expenses_krw": 7_550, "gain_krw": 3_050_000 - 3_030_000 - 7_550}


def test_method_signature_detects_lot_split_as_fifo():
    m = statement.method_signature(statement.load(str(FIX)))
    assert m["sales_split_into_lots"] == 1 and m["securities_fifo_violated"] == 0 and m["guess"] == "FIFO"


def test_method_signature_flags_avg_when_acq_dates_go_backwards(tmp_path):
    """같은 종목 두 매도: 두 번째 매도가 첫 매도보다 이른 취득일 lot 을 쓴다 → 선입선출과 모순 → AVG 신호."""
    import csv
    with FIX.open(encoding="utf-8-sig", newline="") as f:  # 머리글 칸 안에 줄바꿈이 있다 — 줄 단위로 자르면 안 된다
        rows = list(csv.reader(f))
    hdr, a1, _, a3 = rows[0], rows[1], rows[2], rows[3]
    a1 = [v.replace("2025-07-10", "2025-07-20") for v in a1]   # 첫 매도(08-05)가 07-20 lot
    a3 = [v.replace("2025-07-20", "2025-07-10") for v in a3]   # 둘째 매도(09-01)가 더 이른 07-10 lot
    p = tmp_path / "s.csv"
    with p.open("w", encoding="utf-8-sig", newline="") as f:
        csv.writer(f).writerows([hdr, a1, a3])
    m = statement.method_signature(statement.load(str(p)))
    assert m["securities_fifo_violated"] == 1 and m["guess"] == "AVG"
