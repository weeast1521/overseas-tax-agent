"""F07 사후 검증(SCRUM-36). 수용 조건: 지어낸 숫자 / 없는 조문 / '이동평균이 유리합니다' / 정상 + 합성 10건 거짓 양성 0."""
import asyncio
import json

import pytest

from app import main, repo
from app.answer import assemble as asm
from app.answer import verify as vf
from app.judge.rule_judge import RuleJudge
from app.llm.base import Completion, ToolCall
from app.schemas import InquiryRequest

FIG = {"total_gain_krw": 3993764, "deduction_krw": 2500000, "taxable_krw": 1493764, "tax_krw": 328628,
       "by_account": [{"account_id": "MIRAE-1023", "broker_code": "MIRAE", "gain_krw": 3993764, "sales": [{"sold_qty": "10", "proceeds_krw": 9000000, "cost_krw": 5006236, "gain_krw": 3993764, "lots_used": []}]}]}
SRC = [{"law": "소득세법 시행령", "article": "제162조"}]
OK_TEXT = ("먼저 산 주식부터 판 것으로 보아 계산했습니다. 2025년 해외주식 양도차익 합계는 3,993,764원이고, 기본공제 2,500,000원을 뺀 "
           "과세표준 1,493,764원에 세율 22%(지방소득세 포함)를 적용한 양도소득세는 328,628원입니다.\n근거: 소득세법 시행령 제162조")


def test_normal_passes():
    r = vf.verify(OK_TEXT, FIG, SRC)
    assert r.ok, r.failures


def test_rounded_number_fails():
    r = vf.verify(OK_TEXT.replace("328,628원", "330,000원"), FIG, SRC)
    assert "E-VERIFY-NUMBER" in r.codes and any("330,000원" in f["detail"] for f in r.failures)


@pytest.mark.parametrize("bad", ["약 330,000원", "250만 원", "33만 원 정도", "세율 30%"])
def test_approx_and_unit_forms_fail(bad):
    r = vf.verify(OK_TEXT.replace("양도소득세는 328,628원입니다.", f"양도소득세는 {bad}입니다."), FIG, SRC)
    assert "E-VERIFY-NUMBER" in r.codes, bad


def test_unretrieved_article_fails():
    r = vf.verify(OK_TEXT.replace("계산했습니다.", "계산했습니다. [소득세법 제118조의2] 소득세법 시행령 제178조의5에 따릅니다."), FIG, SRC)
    assert "E-VERIFY-SOURCE" in r.codes
    assert {f["detail"].split(" — ")[0] for f in r.failures} == {"[소득세법 제118조의2]", "제178조의5"}  # 제162조는 통과


def test_advice_fails():
    r = vf.verify("이동평균이 유리합니다. 반드시 바꾸세요.", None, [])
    assert "E-VERIFY-ADVICE" in r.codes


def test_internal_leak_and_pii_fail():
    r = vf.verify('{"summary": "tax_calc 결과 확인 필요"} MIRAE-1023 계좌의 FIFO 매도 NAMUH-20250519-7', None, [])
    assert "E-VERIFY-LEAK" in r.codes and "E-VERIFY-PII" in r.codes


def test_length_and_missing_status():
    r = vf.verify("가" * 601, None, [], status_sentences=["확인한 뒤에 안내드립니다."])
    assert set(r.codes) == {"E-VERIFY-LENGTH", "E-VERIFY-STATUS"}


def test_year_month_percent_not_false_positive():
    r = vf.verify("2025년 5월 확정신고 대상이며 세율은 22%입니다.", None, [])
    assert r.ok, r.failures


# --- 조립기 출력 10건: 정상인데 걸리는 것(거짓 양성) 0 ---

class ScriptLLM:
    name = "script"

    def __init__(self):
        self.i = 0

    async def complete(self, messages, tools):
        self.i += 1
        if self.i == 1: return Completion(tool_call=ToolCall(id="1", name="sql_query", args={"intent": "전체"}))
        if self.i == 2: return Completion(tool_call=ToolCall(id="2", name="tax_calc", args={}))
        if self.i == 3: return Completion(tool_call=ToolCall(id="3", name="reg_search", args={"query": "취득가액 선입선출"}))
        if self.i == 4: return Completion(text=json.dumps({"summary": "요약", "figures": {}, "sources": []}))
        return Completion(text="증권사마다 취득가액을 정하는 방식이 다를 수 있어 계산서 금액이 서로 다를 수 있습니다. 양도소득세는 모든 계좌를 합산해 한 번에 신고합니다.")


def test_assembled_drafts_have_no_false_positives(monkeypatch):
    monkeypatch.setattr(main, "_judge", lambda: RuleJudge())
    monkeypatch.setenv("TRADES_FILE", str(repo.DATA / "synthetic" / "trades.json"))
    custs = sorted({int(r["customer_id"]) for r in json.loads(repo.DATA.joinpath("synthetic", "trades.json").read_text(encoding="utf-8"))})[:10]
    bad = []
    for cid in custs:
        monkeypatch.setattr(main, "_llm", lambda: ScriptLLM())
        resp, _ = asyncio.run(main.handle_inquiry(InquiryRequest(customer_id=cid, tax_year=2025, text="두 증권사 계산서가 달라요 합산 어떻게 하나요")))
        if resp.verify_failures:
            bad.append((cid, resp.verify_failures))
        assert resp.draft and resp.review_reason != "VERIFY_FAILED"
    assert not bad, bad


def test_verify_failed_blocks_auto(monkeypatch):
    monkeypatch.setattr(main, "_judge", lambda: RuleJudge())
    monkeypatch.setenv("TRADES_FILE", str(repo.DATA / "samples" / "trades.json"))  # 다른 테스트가 TRADES_FILE을 synthetic으로 바꿔 둘 수 있다

    class Bad(ScriptLLM):
        async def complete(self, messages, tools):
            c = await super().complete(messages, tools)
            return Completion(text="이동평균이 유리하니 바꾸세요. 세금은 약 30만 원입니다.") if self.i >= 5 else c

    monkeypatch.setattr(main, "_llm", lambda: Bad())
    resp, _ = asyncio.run(main.handle_inquiry(InquiryRequest(customer_id=1042, tax_year=2025, text="세금 얼마예요")))  # 1042 = 단일 FIFO 증권사 → 원래 AUTO
    assert resp.status == "REVIEW" and resp.review_reason == "VERIFY_FAILED"
    assert {f["code"] for f in resp.verify_failures} >= {"E-VERIFY-ADVICE", "E-VERIFY-NUMBER"}
    assert "검증 실패" in resp.agent_note and resp.error_code.startswith("E-VERIFY")


def test_decimal_amounts_compared_whole():
    """버그: 12.63달러의 '63'만 잡아 거짓 양성 (JSON 모드 사후 적용에서 발견)."""
    fig = {"by_account": [{"gain_krw": 1, "sales": [{"usd_gain": "12.63", "sold_qty": "1.5", "lots_used": []}]}]}
    assert vf.verify("차익은 12.63달러, 1.5주를 파셨습니다.", fig, []).ok
    assert "E-VERIFY-NUMBER" in vf.verify("차익은 12.64달러입니다.", fig, []).codes


def test_tax_rate_parts_allowed():
    assert vf.verify("세율은 20%에 지방소득세 2%를 더한 22%입니다.", None, []).ok
