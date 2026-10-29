"""폐쇄망 원칙: 로컬 모델이 실패해도 외부(Claude)로 폴백하지 않는다 (SCRUM-51, F02-4·F08-6). 외부 호출 없음.

LLM_PROVIDER=ollama 로 두고 Ollama 를 죽인 상태를 흉내 낸다. anthropic 모듈 자체를 '부르면 터지는' 가짜로 바꿔 두어
어디서든 폴백 시도가 있으면 즉시 드러난다.
"""
from __future__ import annotations

import asyncio
import sys
import types

import httpx
import pytest

from app import egress, main, repo
from app.llm import base as llm_base
from app.schemas import InquiryRequest


@pytest.fixture
def anthropic_forbidden(monkeypatch):
    """anthropic 을 import 하거나 클라이언트를 만들면 실패하는 가짜 모듈."""
    calls = []

    class Boom:
        def __init__(self, *a, **k):
            calls.append("constructed")
            raise AssertionError("폐쇄망 구성에서 anthropic 클라이언트가 만들어짐 — 폴백 금지 위반")

    fake = types.ModuleType("anthropic")
    fake.Anthropic = Boom
    fake.AsyncAnthropic = Boom
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    return calls


@pytest.fixture
def ollama_down(monkeypatch):
    """Ollama 포트가 닫힌 상황: httpx 가 즉시 ConnectError."""
    async def post(self, *a, **k):
        raise httpx.ConnectError("Connection refused")
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_HOST", "http://127.0.0.1:9")  # 닫힌 포트
    monkeypatch.setenv("JUDGE_IMPL", "rule")
    monkeypatch.setenv("TRADES_FILE", str(repo.DATA / "samples" / "trades.json"))
    monkeypatch.setattr(main, "_judge", lambda: main.get_judge())
    monkeypatch.setattr(main, "_llm", lambda: llm_base.get_client())


def test_ollama_failure_returns_E_LLM_and_never_touches_anthropic(anthropic_forbidden, ollama_down):
    n0 = len(egress.log())
    resp, agent = asyncio.run(main.handle_inquiry(InquiryRequest(customer_id=1042, tax_year=2025, text="세금 얼마예요")))
    assert resp.status == "FAILED" and resp.error_code == "E-LLM"
    assert agent.aborted == "E-LLM" and "ConnectError" in (agent.error_detail or "")
    assert resp.figures and resp.figures["tax_krw"] is not None  # 숫자는 모델과 무관하게 나간다
    assert resp.draft  # 코드 문장 초안
    assert anthropic_forbidden == [] and len(egress.log()) == n0  # 외부로 나간 것 없음


def test_claude_provider_is_not_constructed_when_provider_is_ollama(anthropic_forbidden, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    c = llm_base.get_client()
    assert c.name == "ollama" and anthropic_forbidden == []


def test_rule_judge_path_has_no_claude(anthropic_forbidden, monkeypatch):
    monkeypatch.setenv("JUDGE_IMPL", "rule")
    j = main.get_judge()
    assert j.name == "rule" and anthropic_forbidden == []


def test_llm_exception_is_recorded_as_step_not_crash():
    """어떤 LLM 예외든 500 이 아니라 E-LLM 스텝으로 남는다 — 폴백도, 재시도도 없다 (호출 1회)."""
    from app.agent import react
    from app.judge.rule_judge import RuleJudge
    from app.tools import registry

    class Dying:
        name = "ollama"
        calls = 0

        async def complete(self, messages, tools):
            self.calls += 1
            raise RuntimeError("model crashed")

    llm = Dying()
    j = asyncio.run(RuleJudge().classify("세금 얼마예요"))
    res = asyncio.run(react.run("세금 얼마예요", j, llm, registry.ToolContext(1042, 2025)))
    assert res.aborted == "E-LLM" and llm.calls == 1 and res.steps[-1].error_code == "E-LLM"
