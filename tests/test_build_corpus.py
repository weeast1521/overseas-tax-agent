"""SCRUM-31 청킹 — 법제처 API 응답 모양을 본뜬 가짜 JSON과 붙여 넣기 텍스트. 실제 법령 문구는 쓰지 않는다(맥에서 API로 받음)."""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("build_corpus", ROOT / "scripts" / "build_corpus.py")
bc = importlib.util.module_from_spec(spec); spec.loader.exec_module(bc)

FAKE = {"법령": {
    "기본정보": {"법령명_한글": "가상법", "시행일자": "20250101"},
    "조문": {"조문단위": [
        {"조문여부": "전문", "조문번호": "118", "조문내용": "제10절 국외자산 양도"},  # 장·절 제목 → 제외
        {"조문여부": "조문", "조문번호": "118", "조문가지번호": "2", "조문제목": "가상 범위",
         "조문내용": "제118조의2(가상 범위) 가상 머리말이다.",
         "항": [{"항번호": "①", "항내용": "① 첫째 항이다.", "호": [{"호번호": "1.", "호내용": "1. 첫째 호"}, {"호내용": "2. 둘째 호"}]},
               {"항번호": "②", "항내용": "② 대체입고된 주식의 취득가액은 원래 취득한 가액이다. " + "긴 문장이다. " * 150}]},
        {"조문여부": "조문", "조문번호": "103", "조문제목": "가상 공제", "조문내용": "제103조(가상 공제) 연 250만원을 공제한다."},
        {"조문여부": "조문", "조문번호": "200", "조문내용": "제200조(목록에 없음) 무시된다."},
    ]}}}


def test_parse_skips_headings_and_filters_articles():
    base, arts = bc.parse_law_json(FAKE, "가상법", {"제118조의2", "제103조"})
    assert base["시행일자"] == "20250101"
    assert [a["article"] for a in arts] == ["제118조의2", "제103조"]
    a = arts[0]
    assert a["title"] == "가상 범위" and [c[0] for c in a["clauses"]] == ["①", "②"]
    assert "1. 첫째 호" in a["clauses"][0][1] and "2. 둘째 호" in a["clauses"][0][1]  # 호가 항 본문에 붙음
    assert arts[1]["clauses"] == []  # 항 없는 조문


def test_chunks_have_meta_limits_and_context():
    base, arts = bc.parse_law_json(FAKE, "가상법", None)
    cs = bc.law_chunks("가상법", "법령", base, arts, "https://www.law.go.kr/법령/가상법")
    assert all(c["source_url"].startswith("https://www.law.go.kr/") and c["effective_date"] == "2025-01-01" for c in cs)
    assert all(len(c["text"]) <= bc.MAX_CHARS + 210 for c in cs)
    long = [c for c in cs if c["article"] == "제118조의2" and c["clause"] == "②"]
    assert len(long) >= 2 and [c["part"] for c in long] == list(range(1, len(long) + 1))  # 800자 넘으면 문장 경계로 나눔
    assert all(c["text"].startswith("제118조의2(가상 범위)") for c in long)  # 조 머리를 각 chunk 앞에 붙여 맥락 유지
    assert "대체입고" in long[0]["keywords"] and "취득가액" in long[0]["keywords"]
    assert len({c["chunk_id"] for c in cs}) == len(cs)
    one = [c for c in cs if c["article"] == "제103조"]
    assert len(one) == 1 and "250만" in one[0]["text"]


def test_raw_text_sections(tmp_path):
    p = tmp_path / "nts_guide.txt"
    p.write_text("# source_url: https://www.nts.go.kr/example\n# effective_date: 2025-05-01\n# tax_year_scope: 2024 귀속\n"
                 "## 환율 기준\n양도가액은 대금을 수령한 날, 취득가액은 지급한 날의 기준환율로 환산합니다.\n\n## 빈 절\n", encoding="utf-8")
    cs = bc.raw_chunks(p, "국세청 안내", "국세청안내")
    assert len(cs) == 1 and cs[0]["article"] == "환율 기준" and cs[0]["tax_year_scope"] == "2024 귀속"
    assert cs[0]["effective_date"] == "2025-05-01" and cs[0]["text"].startswith("환율 기준\n")


def test_raw_text_requires_source(tmp_path):
    p = tmp_path / "x.txt"
    p.write_text("## 제목\n본문", encoding="utf-8")
    try:
        bc.raw_chunks(p, "x", "x")
        assert False
    except SystemExit as e:
        assert "source_url" in str(e)


def test_check_flags_placeholder_missing_meta_and_required_questions():
    seed = json.loads((ROOT / "data" / "corpus" / "seed.json").read_text(encoding="utf-8"))
    errs = bc.check(seed)
    assert any("★" in e for e in errs) and any("< 60" in e for e in errs)
    good = [{"chunk_id": f"c{i}", "text": "평범한 문장", "source_url": "u", "effective_date": "2025-01-01"} for i in range(60)]
    errs = bc.check(good)
    assert errs == ["'대체입고 취득가액'에 답하는 chunk 없음", "'결제일 환율'에 답하는 chunk 없음", "'250만원 공제'에 답하는 chunk 없음"]
    good[0]["text"] = "대체입고 주식의 취득가액"; good[1]["text"] = "결제일의 기준환율로 환산"; good[2]["text"] = "250만원을 공제"
    assert bc.check(good) == []


def test_amendment_scope():
    assert bc.last_amended("본문 <개정 2001.12.31, 2005.2.19>") == "2005-02-19"
    assert bc.last_amended("본문 <신설 2024. 12. 31.> 다른 <개정 2019.12.31>") == "2024-12-31"
    assert bc.last_amended("표시 없음") is None
    assert bc.scope_of("2005-02-19", 2025) == "2025 귀속 동일(최종 개정 2005-02-19)"
    assert bc.scope_of("2025-12-23", 2025).startswith("2025 귀속 확인 필요")
    assert bc.scope_of(None, 2025).startswith("2025 귀속 동일(개정 표시 없음")  # 표시 없는 항 = 생긴 뒤 바뀌지 않음
    assert bc.last_amended("② 둘째 항", "제1조 … ② 둘째 항 [본조신설 2025. 12. 23.]") == "2025-12-23"  # 조문 단위 표시 상속
    assert bc.last_amended("② 둘째 항 <개정 2010.1.1>", "[전문개정 2009.12.31]") == "2010-01-01"


def test_fx_question_matches_statute_wording():
    """시행령 제178조의5는 '결제일'이라는 말 없이 '수령하거나 지출한 날 현재 … 기준환율'로 쓴다."""
    cs = [{"chunk_id": f"c{i}", "text": "x", "source_url": "u", "effective_date": "d"} for i in range(60)]
    cs[0]["text"] = "양도가액 및 필요경비를 수령하거나 지출한 날 현재 기준환율에 의하여 계산한다"
    assert not any("결제일 환율" in e for e in bc.check(cs))


def test_pdf_pages_to_chunks():
    m = {"law": "국세청 안내", "doc_type": "국세청안내", "source_url": "https://example/x.pdf", "effective_date": "2025-05-01",
         "tax_year_scope": "2024 귀속"}
    pages = [(1, "표지"), (2, "환율 적용\n양도가액은 양도대금을 수령한 날의 기준환율로 환산합니다.  취득가액도   같습니다."),
             (3, "긴 쪽\n" + "문장입니다. " * 200)]
    cs = bc.pages_to_chunks(pages, m)
    assert [c["article"] for c in cs][:1] == ["p.2"] and cs[0]["title"] == "환율 적용"
    assert cs[0]["source_url"].endswith("#page=2") and cs[0]["effective_date"] == "2025-05-01"
    assert "취득가액도 같습니다" in cs[0]["text"]  # 공백 정리
    assert len([c for c in cs if c["article"] == "p.3"]) >= 2 and all(len(c["text"]) <= bc.MAX_CHARS for c in cs)


def test_pdf_title_skips_page_numbers_and_toc():
    m = {"law": "x", "doc_type": "국세청안내", "source_url": "u", "effective_date": "2025-05-01"}
    cs = bc.pages_to_chunks([(16, "- 14 -\nⅠ. 세금문제 ······················ 3\n해외주식 양도소득 계산 방법\n본문이 충분히 길게 이어지는 문장입니다.")], m)
    assert cs[0]["title"] == "해외주식 양도소득 계산 방법"


def test_check_flags_leftover_paste_instructions():
    cs = [{"chunk_id": f"c{i}", "law": "x", "article": "a", "text": "x", "source_url": "u", "effective_date": "d"} for i in range(60)]
    cs[0]["text"] = "대체입고 취득가액\n(페이지의 해당 부분 원문 그대로)"
    assert any("안내문이 남은 조각 1건" in e for e in bc.check(cs))


def test_transfer_question_accepts_method_difference_explanation():
    cs = [{"chunk_id": f"c{i}", "law": "x", "article": "a", "text": "x", "source_url": "u", "effective_date": "d"} for i in range(60)]
    cs[0]["text"] = "결제일 기준환율로 환산"; cs[1]["text"] = "250만원을 공제"
    cs[2]["text"] = "타사 해외주식 대체입고 시 입고 전 증권사가 이동평균법으로 산출한 양도소득금액과 선입선출법 기준 양도소득금액 간에 차이"
    assert bc.check(cs) == []


def test_sections_split_on_headings_and_span_pages():
    pages = [(5, "- 3 -\n□ 해외주식 양도소득세 과세대상\n" + "국외 증권시장에 상장된 주식을 양도하여 발생한 소득입니다. " * 4 +
              "\n□ 양도시기와 취득시기\n원칙적으로 대금을 청산한 날이며"),
             (6, "- 4 -\n결제일을 양도일로 봅니다. " * 9 + "\nQ1. 수수료도 필요경비인가요?\n" + "매도 수수료는 양도비로 필요경비에 해당합니다. " * 3)]
    secs = bc.split_sections(pages)
    assert [s["title"] for s in secs] == ["□ 해외주식 양도소득세 과세대상", "□ 양도시기와 취득시기", "Q1. 수수료도 필요경비인가요?"]
    assert secs[1]["page"] == 5 and "결제일을 양도일로" in secs[1]["body"]  # 쪽을 넘어가는 소제목은 이어 붙임
    m = {"law": "국세청", "doc_type": "국세청안내", "source_url": "u", "effective_date": "2025-05-01"}
    cs = bc.sections_to_chunks(pages, m)
    assert cs[2]["text"].startswith("Q1. 수수료도 필요경비인가요?") and cs[2]["source_url"] == "u#page=6"
    assert all(len(c["text"]) <= bc.MAX_CHARS for c in cs)
