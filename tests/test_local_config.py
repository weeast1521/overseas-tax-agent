"""구성 B(폐쇄망): LocalJudge·OllamaClient — Ollama 없이 httpx 를 가짜로 바꿔 시험 (SCRUM-49). 외부 호출 없음."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.judge import base as judge_base
from app.judge.claude_judge import JudgeError, JudgeFormatError
from app.judge.local_judge import LocalJudge
from app.llm.base import Message, ToolSpec
from app.llm.ollama_client import OllamaClient


class FakeOllama:
    """순서대로 답을 내는 가짜 /api/chat. 요청 본문을 기록한다."""

    def __init__(self, contents: list, tool_calls: list | None = None):
        self.contents, self.tool_calls, self.requests = list(contents), list(tool_calls or []), []

    def install(self, monkeypatch):
        fake = self

        async def post(self_, url, json=None, **kw):
            fake.requests.append({"url": url, "body": json})
            if "fail" in str(fake.contents[0]) and fake.contents[0] == "__fail__":
                fake.contents.pop(0)
                raise httpx.ConnectError("Connection refused")
            content = fake.contents.pop(0)
            msg = {"role": "assistant", "content": content}
            if fake.tool_calls:
                tc = fake.tool_calls.pop(0)
                if tc:
                    msg["tool_calls"] = [{"function": tc}]
            return httpx.Response(200, json={"message": msg, "prompt_eval_count": 100, "eval_count": 20}, request=httpx.Request("POST", url))

        monkeypatch.setattr(httpx.AsyncClient, "post", post)
        return fake


# ---------- LocalJudge ----------

def test_local_judge_parses_json_and_sets_tools(monkeypatch):
    f = FakeOllama(['{"category": "TRANSFER_BASIS", "confidence": 0.82, "flags": []}']).install(monkeypatch)
    j = asyncio.run(LocalJudge(model="qwen2.5:7b").classify("나무로 옮긴 주식 팔았는데 취득가가 이상해요"))
    assert j.category == "TRANSFER_BASIS" and j.confidence == 0.82 and j.required_tools == ["sql_query", "tax_calc", "reg_search"]
    body = f.requests[0]["body"]
    assert body["format"]["type"] == "object" and body["options"]["temperature"] == 0
    assert body["messages"][-1]["content"] == "나무로 옮긴 주식 팔았는데 취득가가 이상해요"  # 민원 문장만 (F02-3)
    assert "1042" not in json.dumps(body) and body["messages"][0]["role"] == "system"


def test_local_judge_retries_once_on_bad_json_then_format_error(monkeypatch):
    FakeOllama(["잘 모르겠습니다", '{"category": "WHAT", "confidence": 1, "flags": []}']).install(monkeypatch)
    with pytest.raises(JudgeFormatError):
        asyncio.run(LocalJudge().classify("x"))
    FakeOllama(["설명만 함", '{"category": "OFFSET", "confidence": 0.9, "flags": []}']).install(monkeypatch)
    j = asyncio.run(LocalJudge().classify("손실이랑 이익 합쳐지나요"))
    assert j.category == "OFFSET"


def test_local_judge_connection_failure_is_E_JUDGE_no_fallback(monkeypatch):
    FakeOllama(["__fail__"]).install(monkeypatch)
    with pytest.raises(JudgeError) as e:
        asyncio.run(LocalJudge().classify("x"))
    assert e.value.code == "E-JUDGE" and "ConnectError" in str(e.value)


def test_local_judge_sampling_confidence_is_agreement_ratio(monkeypatch):
    f = FakeOllama([
        '{"category": "MULTI_BROKER", "confidence": 0.99, "flags": []}',  # 온도 0
        '{"category": "MULTI_BROKER", "confidence": 0.5, "flags": []}',
        '{"category": "TRANSFER_BASIS", "confidence": 0.5, "flags": []}',
        '{"category": "MULTI_BROKER", "confidence": 0.5, "flags": []}',
        '{"category": "MULTI_BROKER", "confidence": 0.5, "flags": []}',
    ]).install(monkeypatch)
    j = asyncio.run(LocalJudge(samples=5).classify("두 증권사 계산서 합산"))
    assert j.category == "MULTI_BROKER" and j.confidence == 0.8  # 5표 중 4표 — 자기보고 0.99 는 버린다
    assert [r["body"]["options"]["temperature"] for r in f.requests] == [0, 0.7, 0.7, 0.7, 0.7]


def test_local_judge_flags_investment_advice(monkeypatch):
    FakeOllama(['{"category": "OUT_OF_SCOPE", "confidence": 0.95, "flags": ["ASKS_INVESTMENT_ADVICE", "junk"]}']).install(monkeypatch)
    j = asyncio.run(LocalJudge().classify("지금 엔비디아 사도 될까요"))
    assert j.gate() == "REJECTED" and j.flags == ["ASKS_INVESTMENT_ADVICE"]


def test_get_judge_local(monkeypatch):
    monkeypatch.setenv("JUDGE_IMPL", "local")
    monkeypatch.setenv("LOCAL_JUDGE_MODEL", "exaone3.5:7.8b")
    j = judge_base.get_judge()
    assert j.name == "local" and j.model == "exaone3.5:7.8b"


# ---------- OllamaClient ----------

SPECS = [ToolSpec(name="sql_query", description="d", input_schema={"type": "object", "properties": {"intent": {"type": "string"}}, "required": ["intent"]}),
         ToolSpec(name="tax_calc", description="d", input_schema={"type": "object", "properties": {}})]


def test_ollama_native_tool_call_and_string_arguments(monkeypatch):
    f = FakeOllama(["", ""], tool_calls=[{"name": "sql_query", "arguments": {"intent": "전체"}}, {"name": "tax_calc", "arguments": "{}"}]).install(monkeypatch)
    c = OllamaClient(model="qwen2.5:7b")
    r1 = asyncio.run(c.complete([Message(role="user", content="q")], SPECS))
    r2 = asyncio.run(c.complete([Message(role="user", content="q")], SPECS))
    assert r1.tool_call.name == "sql_query" and r1.tool_call.args == {"intent": "전체"}
    assert r2.tool_call.name == "tax_calc" and r2.tool_call.args == {}
    body = f.requests[0]["body"]
    assert body["options"] == {"temperature": 0, "num_ctx": 16384} and [t["function"]["name"] for t in body["tools"]] == ["sql_query", "tax_calc"]


def test_ollama_text_json_tool_call_is_recovered(monkeypatch):
    FakeOllama(['거래를 먼저 조회합니다.\n```json\n{"name": "sql_query", "arguments": {"intent": "2025년 거래"}}\n```']).install(monkeypatch)
    c = OllamaClient()
    r = asyncio.run(c.complete([Message(role="user", content="q")], SPECS))
    assert r.tool_call is not None and r.tool_call.name == "sql_query" and r.text is None
    assert c.last_usage["parsed_from_text"] == 1


def test_ollama_plain_text_is_final(monkeypatch):
    FakeOllama(["최종 메모: 이체 lot 확인 필요"]).install(monkeypatch)
    r = asyncio.run(OllamaClient().complete([Message(role="user", content="q")], SPECS))
    assert r.tool_call is None and r.final and "이체" in r.text


def test_ollama_unknown_tool_in_text_stays_text(monkeypatch):
    FakeOllama(['{"name": "broker_method", "arguments": {}}']).install(monkeypatch)
    r = asyncio.run(OllamaClient().complete([Message(role="user", content="q")], SPECS))
    assert r.tool_call is None and r.text


def test_ollama_down_raises_for_react_to_record_E_LLM(monkeypatch):
    FakeOllama(["__fail__"]).install(monkeypatch)
    with pytest.raises(httpx.ConnectError):
        asyncio.run(OllamaClient().complete([Message(role="user", content="q")], SPECS))


def test_ollama_switches_to_text_tools_when_model_rejects_tools(monkeypatch):
    """exaone3.5 처럼 tools 파라미터에 400 을 내는 모델: 같은 모델로 텍스트 모드 재요청, 본문 JSON 을 툴 호출로. 다른 모델 폴백 아님."""
    calls = []

    async def post(self_, url, json=None, **kw):
        calls.append(json)
        if "tools" in json:
            return httpx.Response(400, json={"error": "registry.ollama.ai/library/exaone3.5:7.8b does not support tools"}, request=httpx.Request("POST", url))
        return httpx.Response(200, json={"message": {"role": "assistant", "content": '{"name": "sql_query", "arguments": {"intent": "전체"}}'}}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    c = OllamaClient(model="exaone3.5:7.8b")
    r = asyncio.run(c.complete([Message(role="system", content="S"), Message(role="user", content="q")], SPECS))
    assert r.tool_call.name == "sql_query" and c.text_tools == "always" and c.last_usage.get("text_mode")
    assert len(calls) == 2 and "tools" not in calls[1] and "[툴 사용 규칙]" in calls[1]["messages"][0]["content"]
    assert all(b["model"] == "exaone3.5:7.8b" for b in calls)
    # 다음 호출부터는 바로 텍스트 모드 (400 안 거침). tool 메시지는 user 턴 "[툴 결과]" 로
    r2 = asyncio.run(c.complete([Message(role="user", content="q"), Message(role="assistant", content='{"intent": "x"}', name="sql_query", tool_call_id="1"),
                                 Message(role="tool", content='{"rows": []}', tool_call_id="1")], SPECS))
    assert len(calls) == 3 and calls[2]["messages"][-1] == {"role": "user", "content": "[툴 결과]\n{\"rows\": []}"}
