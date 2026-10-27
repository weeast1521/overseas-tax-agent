"""F01-6 동기 타임아웃 (SCRUM-19). 느린 LLM 스텁으로 재현, 테스트 타임아웃은 0.5초.

수용 조건: 타임아웃 안에 응답 · status=FAILED · error_code=E-TIMEOUT · trace_id 있음 · 타임아웃 전까지의 스텝이 trace에 남음.
추가: 타임아웃이어도 숫자(figures)와 코드가 조립한 숫자·상태 문장은 상담원에게 간다.
"""
import asyncio
import json
import time

from app import main, repo, trace
from app.judge.rule_judge import RuleJudge
from app.llm.base import Completion, ToolCall
from app.schemas import InquiryRequest


class SlowAfter:
    """fast번째 호출까지는 즉시, 그 뒤로는 sleep초 대기 (모델이 멈춘 상황)."""
    name = "slow"

    def __init__(self, fast: int, sleep: float = 5.0):
        self.fast, self.sleep, self.i = fast, sleep, 0

    async def complete(self, messages, tools):
        self.i += 1
        if self.i > self.fast:
            await asyncio.sleep(self.sleep)
        if self.i == 1:
            return Completion(tool_call=ToolCall(id="1", name="sql_query", args={"intent": "전체"}), in_tokens=10, out_tokens=5)
        if self.i == 2:
            return Completion(tool_call=ToolCall(id="2", name="tax_calc", args={}), in_tokens=10, out_tokens=5)
        if self.i == 3:
            return Completion(text="상담원 메모: 단일 증권사, 이체 없음.", in_tokens=10, out_tokens=5)
        return Completion(text="먼저 산 주식부터 판 것으로 계산했습니다.", in_tokens=10, out_tokens=5)  # 조립기


def _run(monkeypatch, llm, cid=1042, text="세금 얼마예요"):
    monkeypatch.setenv("SYNC_TIMEOUT_SEC", "0.5")
    monkeypatch.setenv("TRADES_FILE", str(repo.DATA / "samples" / "trades.json"))
    monkeypatch.setattr(main, "_judge", lambda: RuleJudge())
    monkeypatch.setattr(main, "_llm", lambda: llm)
    t0 = time.perf_counter()
    resp, agent = asyncio.run(main.handle_inquiry(InquiryRequest(customer_id=cid, tax_year=2025, text=text)))
    return resp, agent, time.perf_counter() - t0


def test_agent_timeout_failed_with_partial_trace(monkeypatch):
    resp, agent, took = _run(monkeypatch, SlowAfter(fast=2))  # sql_query·tax_calc 후 3번째 호출에서 멈춤
    assert took < 1.5  # 5초 기다리지 않는다
    assert resp.status == "FAILED" and resp.error_code == "E-TIMEOUT" and resp.trace_id.startswith("tr_")
    assert resp.figures and resp.figures["tax_krw"] >= 0  # 숫자는 에이전트와 독립
    assert f"{resp.figures['tax_krw']:,}원" in resp.draft  # 코드가 조립한 숫자 문장
    assert "E-TIMEOUT" in resp.agent_note and "스텝 2까지" in resp.agent_note and "tax_calc" in resp.agent_note
    t = trace.get_store().get(resp.trace_id)
    assert t["status"] == "FAILED" and t["error_code"] == "E-TIMEOUT"
    assert [s["tool"] for s in t["steps_detail"]] == ["sql_query", "tax_calc"]  # 타임아웃 전까지의 스텝


def test_assembler_timeout_falls_back_to_code_only(monkeypatch):
    resp, agent, took = _run(monkeypatch, SlowAfter(fast=3))  # 에이전트는 끝나고 조립 LLM에서 멈춤
    assert took < 1.5
    # 에이전트는 끝났으니 FAILED 가 아니다 — 코드 문장만으로 내보내고 AUTO 는 REVIEW/ASSEMBLER_TIMEOUT 로 (10/27)
    assert resp.status == "REVIEW" and resp.review_reason == "ASSEMBLER_TIMEOUT" and resp.error_code == "E-ASSEMBLE-TIMEOUT"
    assert agent.final and agent.aborted is None and len(agent.steps) == 3
    assert f"{resp.figures['tax_krw']:,}원" in resp.draft and "먼저 산 주식부터 판 것으로 계산했습니다" not in resp.draft
    assert "E-ASSEMBLE-TIMEOUT" in resp.agent_note


def test_fast_path_unaffected(monkeypatch):
    resp, agent, took = _run(monkeypatch, SlowAfter(fast=99))
    assert resp.status == "AUTO" and resp.error_code is None and "먼저 산 주식부터 판 것으로 계산했습니다" in resp.draft
