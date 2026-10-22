"""F03-6 agent_trace — 저장·조회·실패 격리. RuleJudge + 가짜 LLM."""
import json

import pytest
from fastapi.testclient import TestClient

from app import main, trace
from app.agent import react
from app.judge.rule_judge import RuleJudge
from app.llm.base import Completion, ToolCall
from app.trace import SQLiteTraceStore


class FakeLLM:
    name = "fake"
    model = "fake-model"

    def __init__(self, script):
        self.script = list(script)

    async def complete(self, messages, tools):
        return self.script.pop(0)


def happy():
    return FakeLLM([
        Completion(tool_call=ToolCall(id="1", name="sql_query", args={"intent": "전체"}), in_tokens=100, out_tokens=10),
        Completion(tool_call=ToolCall(id="2", name="tax_calc", args={}), in_tokens=200, out_tokens=20),
        Completion(text=json.dumps({"summary": "요약", "figures": {}, "sources": []}), in_tokens=300, out_tokens=30),
    ])


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main, "_judge", lambda: RuleJudge())
    monkeypatch.setattr(main, "_llm", happy)
    return TestClient(main.app)


def test_store_roundtrip(tmp_path):
    s = SQLiteTraceStore(tmp_path / "t.db")
    s.save({"trace_id": "tr_a", "customer_id": 1, "tax_year": 2025, "inquiry_text": "q", "category": "TAX_BASIS",
            "confidence": 0.9, "status": "AUTO", "steps": 1, "tokens_in": 5, "tokens_out": 1, "latency_ms": 7},
           [{"step_no": 0, "tool": "sql_query", "args_json": '{"intent": "x"}', "observation": "o", "latency_ms": 3,
             "in_tokens": 5, "out_tokens": 1, "off_plan": 0}])
    t = s.get("tr_a")
    assert t["status"] == "AUTO" and t["steps_detail"][0]["args"] == {"intent": "x"} and t["steps_detail"][0]["off_plan"] is False
    assert s.get("nope") is None
    assert [r["trace_id"] for r in s.list(status="AUTO")] == ["tr_a"] and s.list(status="REVIEW") == []


def test_api_saves_trace_with_steps_and_tokens(client):
    r = client.post("/v1/inquiries", json={"customer_id": 1042, "tax_year": 2025, "text": "세금 얼마예요"})
    j = r.json()
    t = client.get(f"/v1/traces/{j['trace_id']}").json()
    assert t["status"] == j["status"] and t["category"] == "TAX_BASIS" and t["gate"] in ("RUN_AUTO", "RUN_REVIEW")
    assert t["judge_name"] == "rule" and t["llm_name"] == "fake" and t["llm_model"] == "fake-model"
    assert t["steps"] == 3 and t["tokens_in"] == 600 and t["tokens_out"] == 60 and j["tokens_in"] == 600
    assert [s["tool"] for s in t["steps_detail"]] == ["sql_query", "tax_calc", None]
    assert '"tax_krw"' in t["steps_detail"][1]["observation"] and t["final_text"] == j["draft"]
    assert t["inquiry_text"] == "세금 얼마예요"


def test_rejected_and_low_confidence_are_recorded_without_steps(client):
    rej = client.post("/v1/inquiries", json={"customer_id": 1042, "tax_year": 2025, "text": "테슬라 사도 될까요 추천"}).json()
    low = client.post("/v1/inquiries", json={"customer_id": 1042, "tax_year": 2025, "text": "안녕하세요 문의드립니다"}).json()
    t1 = client.get(f"/v1/traces/{rej['trace_id']}").json()
    t2 = client.get(f"/v1/traces/{low['trace_id']}").json()
    assert t1["status"] == "REJECTED" and t1["gate"] == "REJECTED" and t1["steps_detail"] == []
    assert t2["status"] == "REVIEW" and t2["review_reason"] == "LOW_CONFIDENCE" and t2["steps"] == 0
    items = client.get("/v1/traces", params={"status": "REVIEW"}).json()["items"]
    assert low["trace_id"] in [i["trace_id"] for i in items] and rej["trace_id"] not in [i["trace_id"] for i in items]


def test_unknown_trace_404(client):
    r = client.get("/v1/traces/tr_nope")
    assert r.status_code == 404 and r.json()["error"] == "E-NOT-FOUND"


def test_store_failure_does_not_break_response(client, monkeypatch):
    class Broken:
        def save(self, *_): raise RuntimeError("disk full")
    monkeypatch.setattr(trace, "_store", Broken())
    r = client.post("/v1/inquiries", json={"customer_id": 1042, "tax_year": 2025, "text": "세금 얼마예요"})
    assert r.status_code == 200 and r.json()["status"] in ("AUTO", "REVIEW")


def test_prompt_injects_judgment():
    """F03-6: 유형·필요 툴·확신도가 시스템 프롬프트에 들어간다."""
    seen = {}

    class Spy:
        name = "spy"

        async def complete(self, messages, tools):
            seen["system"] = messages[0].content
            return Completion(text="{}")

    import asyncio
    from app.judge.base import Judgment
    from app.tools import registry
    j = Judgment(category="TRANSFER_BASIS", confidence=0.87, required_tools=["sql_query", "broker_method"])
    asyncio.run(react.run("x", j, Spy(), registry.ToolContext(1077, 2025)))
    assert "TRANSFER_BASIS" in seen["system"] and "0.87" in seen["system"] and "sql_query, broker_method" in seen["system"]
