"""F01 API 테스트 — RuleJudge + 가짜 LLM. 외부 호출 없음."""
import json

import pytest
from fastapi.testclient import TestClient

from app import main
from app.judge.rule_judge import RuleJudge
from app.llm.base import Completion, ToolCall


class FakeLLM:
    name = "fake"

    def __init__(self, script):
        self.script = list(script)

    async def complete(self, messages, tools):
        return self.script.pop(0) if self.script else Completion(text=json.dumps({"summary": "끝", "figures": {}, "sources": []}))


def happy():
    return FakeLLM([
        Completion(tool_call=ToolCall(id="1", name="sql_query", args={"intent": "전체"})),
        Completion(tool_call=ToolCall(id="2", name="tax_calc", args={})),
        Completion(text=json.dumps({"summary": "요약", "figures": {}, "sources": []})),
    ])


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main, "_judge", lambda: RuleJudge())
    monkeypatch.setattr(main, "_llm", happy)
    return TestClient(main.app)


def post(client, **kw):
    body = {"customer_id": 1023, "tax_year": 2025, "text": "두 증권사 계산서가 달라요 합산 어떻게 하나요", "mode": "sync"}
    body.update(kw)
    return client.post("/v1/inquiries", json=body)


def test_schema_filled_and_transfer_review_for_1077(client):
    r = post(client, customer_id=1077, text="미래에셋에서 산 테슬라를 나무에서 팔았는데 두 계산서가 달라요")
    assert r.status_code == 200
    j = r.json()
    assert j["trace_id"].startswith("tr_")
    assert j["category"] == "MULTI_BROKER" and j["confidence"] > 0
    assert len(j["method"]["brokers"]) == 2  # MIRAE(매수·출고만), NAMUH(입고·매도)
    assert [a["account_id"] for a in j["figures"]["by_account"]] == ["NAMUH-1077"]  # 매도 있는 계좌만 계산내역서
    assert j["figures"]["transferred_lot_sales"]  # 이체 lot 포함 매도
    assert set(j["method"]["brokers"].values()) == {"FIFO", "CHOICE"}
    # NAMUH는 선택제인데 override 없음 → METHOD_UNCONFIRMED가 TRANSFER_REVIEW보다 우선
    assert j["status"] == "REVIEW" and j["review_reason"] == "METHOD_UNCONFIRMED"
    assert j["method"]["needs_confirmation"] == ["NAMUH-1077"]
    assert j["draft"] and j["steps"] == 3


def test_override_clears_confirmation_then_transfer_review(client):
    r = post(client, customer_id=1077, method_override="AVG",
             text="미래에셋에서 산 테슬라를 나무에서 팔았는데 두 계산서가 달라요")
    j = r.json()
    assert j["method"]["applied"] == "AVG" and j["method"]["needs_confirmation"] == []
    assert j["status"] == "REVIEW" and j["review_reason"] == "TRANSFER_REVIEW"
    assert j["figures"]["by_account"][0]["method"] == "AVG"


def test_single_fifo_broker_auto(client):
    r = post(client, customer_id=1042, text="세금 얼마예요")
    j = r.json()
    assert j["status"] == "AUTO" and j["review_reason"] is None
    assert j["method"]["needs_confirmation"] == []
    assert j["figures"]["tax_krw"] >= 0


def test_wrong_tax_year_400(client):
    r = post(client, tax_year=2024)
    assert r.status_code == 400 and r.json()["error"] == "E-VALIDATION"


def test_async_mode_400(client):
    r = post(client, mode="async")
    assert r.status_code == 400 and r.json()["error"] == "E-VALIDATION"


def test_rejected_still_has_category_and_trace(client):
    r = post(client, text="테슬라 지금 사도 될까요 추천해주세요")
    j = r.json()
    assert j["status"] == "REJECTED" and j["category"] == "OUT_OF_SCOPE" and j["trace_id"]


def test_low_confidence_review(client):
    r = post(client, customer_id=1042, text="안녕하세요 문의드립니다")
    j = r.json()
    assert j["status"] == "REVIEW" and j["review_reason"] == "LOW_CONFIDENCE" and j["confidence"] < 0.7


def test_agent_abort_becomes_failed(client, monkeypatch):
    bad = Completion(text=None, tool_call=None)
    monkeypatch.setattr(main, "_llm", lambda: FakeLLM([bad, bad]))
    r = post(client, customer_id=1042, text="세금 얼마예요")
    j = r.json()
    assert j["status"] == "FAILED" and j["error_code"] == "E-LLM-FORMAT"
    assert j["figures"] is not None  # 숫자는 LLM과 무관하게 나온다


def test_no_trades_customer(client):
    r = post(client, customer_id=9999, text="세금 얼마예요")
    j = r.json()
    assert j["figures"] is None and j["error_code"] == "E-NO-TRADES"
