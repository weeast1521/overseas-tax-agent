"""ClaudeJudge 테스트 — 가짜 Anthropic 클라이언트. API 호출 없음."""
import asyncio
import json
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from app import main
from app.judge.base import DEFAULT_TOOLS
from app.judge.claude_judge import CATEGORIES, ClaudeJudge, JudgeError, JudgeFormatError


def tool_resp(inp, in_tokens=900, out_tokens=40):
    return NS(content=[NS(type="tool_use", name="classify", input=inp)],
              usage=NS(input_tokens=in_tokens, output_tokens=out_tokens))


class FakeMessages:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    async def create(self, **req):
        self.requests.append(req)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def judge_with(*replies):
    fake = FakeMessages(replies)
    return ClaudeJudge(model="test-model", client=NS(messages=fake)), fake


def test_valid_output_sets_default_tools_and_usage():
    j, fake = judge_with(tool_resp({"category": "TRANSFER_BASIS", "confidence": 0.93, "flags": []}))
    r = asyncio.run(j.classify("다른 증권사에서 옮긴 주식 취득가가 이상해요"))
    assert r.category == "TRANSFER_BASIS" and r.confidence == 0.93
    assert r.required_tools == DEFAULT_TOOLS["TRANSFER_BASIS"]
    assert j.last_usage == {"calls": 1, "in_tokens": 900, "out_tokens": 40}
    req = fake.requests[0]
    assert req["tool_choice"] == {"type": "tool", "name": "classify"} and req["temperature"] == 0
    assert req["tools"][0]["input_schema"]["properties"]["category"]["enum"] == CATEGORIES


def test_schema_violation_retries_once_then_succeeds():
    j, fake = judge_with(tool_resp({"category": "STOCK_TIP", "confidence": 0.9, "flags": []}),
                         tool_resp({"category": "OFFSET", "confidence": 0.8, "flags": []}))
    r = asyncio.run(j.classify("손실이랑 이익 합쳐서 계산하나요"))
    assert r.category == "OFFSET"
    assert len(fake.requests) == 2 and "스키마를 벗어났다" in fake.requests[1]["system"]
    assert j.last_usage["calls"] == 2 and j.last_usage["in_tokens"] == 1800


def test_schema_violation_twice_is_llm_format():
    bad = {"category": "TAX_BASIS", "confidence": 1.7, "flags": []}
    j, _ = judge_with(tool_resp(bad), tool_resp(bad))
    with pytest.raises(JudgeFormatError) as e:
        asyncio.run(j.classify("세금 얼마예요"))
    assert e.value.code == "E-LLM-FORMAT"


def test_no_tool_use_block_counts_as_format_error():
    text_only = NS(content=[NS(type="text", text="TAX_BASIS")], usage=NS(input_tokens=1, output_tokens=1))
    j, _ = judge_with(text_only, text_only)
    with pytest.raises(JudgeFormatError):
        asyncio.run(j.classify("세금 얼마예요"))


def test_api_failure_is_e_judge_without_retry():
    j, fake = judge_with(RuntimeError("401 invalid x-api-key"))
    with pytest.raises(JudgeError) as e:
        asyncio.run(j.classify("세금 얼마예요"))
    assert e.value.code == "E-JUDGE" and not isinstance(e.value, JudgeFormatError)
    assert len(fake.requests) == 1  # 폴백·재시도 없음 (F02-4)


def test_request_contains_only_inquiry_text():
    j, _ = judge_with()
    req = j.build_request("세금 얼마예요")
    assert req["messages"] == [{"role": "user", "content": "세금 얼마예요"}]


def test_api_never_sends_customer_identifiers_to_judge(monkeypatch):
    """F02-3: /v1/inquiries 경로에서 Judge 요청에 customer_id·계좌번호가 섞이지 않는다."""
    fake = FakeMessages([tool_resp({"category": "OUT_OF_SCOPE", "confidence": 0.9, "flags": ["ASKS_INVESTMENT_ADVICE"]})])
    judge = ClaudeJudge(model="test-model", client=NS(messages=fake))
    monkeypatch.setattr(main, "_judge", lambda: judge)
    r = TestClient(main.app).post("/v1/inquiries", json={"customer_id": 1077, "tax_year": 2025,
                                                         "text": "테슬라 지금 사도 될까요"})
    assert r.json()["status"] == "REJECTED"
    sent = json.dumps(fake.requests, ensure_ascii=False)
    assert "1077" not in sent and "NAMUH" not in sent and "MIRAE" not in sent


def test_judge_failure_maps_error_code_in_api(monkeypatch):
    j, _ = judge_with(tool_resp({"category": "X", "confidence": 0.9, "flags": []}),
                      tool_resp({"category": "X", "confidence": 0.9, "flags": []}))
    monkeypatch.setattr(main, "_judge", lambda: j)
    r = TestClient(main.app).post("/v1/inquiries", json={"customer_id": 1042, "tax_year": 2025, "text": "세금 얼마예요"})
    body = r.json()
    assert body["status"] == "REVIEW" and body["review_reason"] == "AGENT_ABORTED" and body["error_code"] == "E-LLM-FORMAT"


def test_judge_impl_env_switch(monkeypatch):
    """JUDGE_IMPL=claude면 다른 코드 변경 없이 ClaudeJudge가 선택된다."""
    from app.judge import base
    monkeypatch.setenv("JUDGE_IMPL", "claude")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert isinstance(base.get_judge(), ClaudeJudge)
