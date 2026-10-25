"""F07 조립기(SCRUM-35) — 가짜 LLM. 외부 호출 없음.

보는 것: 숫자 문자열이 입력 그대로 / 상태 문장은 LLM이 안 써도 코드가 넣는다 / 인용은 reg_search 집합만 /
LLM이 죽어도 숫자·상태는 나간다 / 금지 단어 없음 / 고객 문장과 상담원 메모 분리.
"""
import asyncio

import pytest

from app import main, repo
from app.answer import assemble as asm
from app.answer import skeleton as sk
from app.llm.base import Completion

CHUNKS = [{"chunk_id": "c_fifo", "law": "소득세법 시행령", "article": "제162조", "text": "..."},
          {"chunk_id": "c_fifo", "law": "소득세법 시행령", "article": "제162조", "text": "중복"}]


class EchoLLM:
    name = "echo"

    def __init__(self, text="두 증권사의 계산 방식이 달라 계산서 금액이 다를 수 있습니다. [소득세법 시행령 제162조]"):
        self.text, self.calls = text, []

    async def complete(self, messages, tools):
        self.calls.append(messages)
        return Completion(text=self.text, in_tokens=100, out_tokens=40)


class DeadLLM:
    name = "dead"

    async def complete(self, messages, tools):
        raise RuntimeError("boom")


def figures_for(cid: int, override=None):
    m = repo.resolve_methods(cid, override)
    fig, err = main.compute_figures(cid, 2025, m["methods"])
    return fig, err, m["needs_confirmation"]


def run(coro):
    return asyncio.run(coro)


def test_1077_transfer_draft_has_all_slots():
    """수용 조건: 고객 1077 — 미래에셋 취득가액 / 나무 계산서와 다를 수 있음 / 숫자 / 출처."""
    fig, err, needs = figures_for(1077, override="FIFO")  # 방식 확정 후 → TRANSFER_REVIEW만 남는다
    assert err is None and not needs and fig["transferred_lot_sales"]
    llm = EchoLLM()
    d = run(asm.assemble("TRANSFER_BASIS", "두 계산서가 달라요", fig, CHUNKS, needs, llm=llm, tax_year=2025))
    t = d.customer_text
    assert "미래에셋증권에서 매수한 취득가액" in t and "NH투자증권(나무) 계산서와 금액이 다를 수 있습니다" in t
    assert f"{fig['tax_krw']:,}원" in t and f"{fig['total_gain_krw']:,}원" in t  # 숫자 문자열 입력 그대로
    assert "근거: 소득세법 시행령 제162조" in d.agent_note and "근거:" not in t and "[" not in t  # 인용은 메모·sources로 (v2)
    assert d.sources == [{"law": "소득세법 시행령", "article": "제162조", "chunk_id": "c_fifo"}]
    assert len(t) <= sk.MAX_CUSTOMER_TEXT
    # LLM 프롬프트에 골격·숫자·조문·상태가 들어갔다
    user = llm.calls[0][1].content
    assert "1. 다른 증권사에서 옮겨 온" in user and f"{fig['tax_krw']:,}원" in user and "소득세법 시행령 제162조: " in user and "옮겨 온 주식" in user
    assert d.tokens_in == 100 and d.tokens_out == 40 and d.error is None


def test_method_unconfirmed_sentence_inserted_by_code_not_llm():
    fig, err, needs = figures_for(1077)  # override 없음 → NAMUH 선택제 미확정
    assert needs == ["NAMUH-1077"]
    d = run(asm.assemble("TRANSFER_BASIS", "…", fig, [], needs, llm=EchoLLM("설명만 씁니다."), tax_year=2025))
    assert "NH투자증권(나무) 계좌는 취득가액 산정 방식을 고객님께서 선택하시는" in d.customer_text  # LLM은 안 썼지만 들어 있다
    assert d.customer_text.count("확인한 뒤에 정확한 금액") == 1  # 한 번만
    assert "방식 미확정 계좌: NAMUH-1077" in d.agent_note and "NAMUH-1077" not in d.customer_text  # 계좌 ID는 메모에만
    assert "근거:" not in d.customer_text and "인용 조문 없음" in d.agent_note


def test_llm_failure_still_delivers_numbers_and_status():
    fig, err, needs = figures_for(1077)
    d = run(asm.assemble("MULTI_BROKER", "…", fig, CHUNKS, needs, llm=DeadLLM(), tax_year=2025))
    assert d.error == "E-ASSEMBLE:RuntimeError" and d.llm_text is None
    assert f"{fig['tax_krw']:,}원" in d.customer_text and "선택하시는 증권사" in d.customer_text and "근거: 소득세법 시행령 제162조" in d.agent_note
    assert "조립 LLM 실패" in d.agent_note


def test_no_llm_and_no_figures_short_case():
    d = run(asm.assemble("TAX_BASIS", "…", None, [], [], llm=None, calc_error="E-CALC-SHORT", tax_year=2025))
    assert d.customer_text == sk.STATUS_SENTENCES["SHORT"]
    assert "계산 오류 E-CALC-SHORT" in d.agent_note


def test_llm_machine_traces_are_cleaned_and_truncated():
    long = "```json\n{\"x\":1}\n```\n설명: **굵게** 첫 문장입니다.\n둘째 줄입니다. " + "가" * 500
    d = run(asm.assemble("TAX_BASIS", "…", None, [], [], llm=EchoLLM(long), tax_year=2025))
    assert "```" not in d.customer_text and "**" not in d.customer_text and "\n둘째" not in d.customer_text
    assert len(d.customer_text) <= sk.MAX_LLM_PART


def test_summary_extracted_from_agent_json_but_not_copied():
    d = run(asm.assemble("OFFSET", "…", None, [], [], llm=EchoLLM("설명."), agent_final='{"summary": "내부 요약 reg_search 2회", "figures": {}, "sources": []}', tax_year=2025))
    assert "에이전트 요약: 내부 요약 reg_search 2회" in d.agent_note and "reg_search" not in d.customer_text


@pytest.mark.parametrize("cid", [1023, 1077])
def test_no_banned_words_in_code_sentences(cid):
    fig, err, needs = figures_for(cid)
    d = run(asm.assemble("MULTI_BROKER", "…", fig, [], needs, llm=None, tax_year=2025))
    for w in ("약 ", "유리", "불리", "추천", "반드시", "무조건", "{", "}", "FIFO", "lot"):
        assert w not in d.customer_text, w


def test_skeletons_cover_all_in_scope_categories_and_have_number_slot():
    for cat in ("TRANSFER_BASIS", "MULTI_BROKER", "TAX_BASIS", "FX_BASIS", "OFFSET", "FILING"):
        assert "[숫자]" in sk.SKELETONS[cat], cat


def test_numbers_sentence_zero_gain():
    s = sk.numbers_sentence({"total_gain_krw": -120000, "deduction_krw": 0, "taxable_krw": 0, "tax_krw": 0}, 2025)
    assert "-120,000원" in s and "0원" in s and "22%" not in s


# --- 조립기 v1 채점(10/25)에서 나온 감점 사유 회귀 테스트 ---

def test_v1_regressions_no_mid_sentence_cut_and_length_budget():
    """v1: 350자에서 글자 단위로 잘라 문장이 끊김 + 코드 문장을 더해 600자 초과 5/19건."""
    fig, err, needs = figures_for(1077)  # 상태 문장 2개 → 코드 몫이 가장 긴 경우
    long = "첫째 설명입니다. " * 10 + "둘째 설명은 이렇게 끝나지 않고 계속"
    d = run(asm.assemble("TRANSFER_BASIS", "…", fig, CHUNKS, needs, llm=EchoLLM(long), tax_year=2025))
    head = d.customer_text.split(" 증권사별")[0].split(" 2025년")[0]
    assert head.endswith("다.") and "계속" not in d.customer_text  # 문장 끝에서 잘림
    assert len(d.customer_text) <= sk.MAX_CUSTOMER_TEXT
    assert asm.llm_budget(d.facts) + len(asm.code_part(d.facts)) <= sk.MAX_CUSTOMER_TEXT


def test_v1_regressions_bracket_citations_removed_but_still_verified():
    from app.answer import verify as vf
    fig, err, needs = figures_for(1077, override="FIFO")
    d = run(asm.assemble("TRANSFER_BASIS", "…", fig, CHUNKS, needs, llm=EchoLLM("옮겨 온 주식은 원래 가격이 적용됩니다. [소득세법 제999조]"), tax_year=2025))
    assert "[" not in d.customer_text and "제999조" not in d.customer_text
    r = vf.verify(d.customer_text, fig, d.sources, d.facts["status_sentences"], raw_llm=d.llm_text)
    assert "E-VERIFY-SOURCE" in r.codes  # 지운 토큰도 원문으로 잡는다


def test_prompt_gives_chunk_text_not_just_names():
    llm = EchoLLM()
    fig, err, needs = figures_for(1077, override="FIFO")
    run(asm.assemble("TRANSFER_BASIS", "…", fig, CHUNKS, needs, llm=llm, tax_year=2025))
    user = llm.calls[0][1].content
    assert "소득세법 시행령 제162조: ..." in user and "★" not in user


def test_numbers_sentence_within_deduction():
    s = sk.numbers_sentence({"total_gain_krw": 1027468, "deduction_krw": 1027468, "taxable_krw": 0, "tax_krw": 0}, 2025)
    assert s == "2025년 해외주식 양도차익 합계는 1,027,468원으로 기본공제 한도 안이라 납부할 양도소득세는 0원입니다."
