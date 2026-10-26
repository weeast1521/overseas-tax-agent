"""F11-2~4 고객 산정 방식 저장 (SCRUM-47). 고객 1077: MIRAE(FIFO 고정, 매수·출고) + NAMUH(선택제, 입고·매도).

수용 조건
  1. NAMUH 기록 없음 → REVIEW/METHOD_UNCONFIRMED, figures FIFO, needs_confirmation=[NAMUH-1077]
  2. set_method(AVG) 후 같은 민원 → AVG로 계산, needs_confirmation 비어 있음
  3. 같은 계좌·연도 set_method(FIFO) 다시 → E-METHOD-CHANGE
  4. 초안에 "나무 계좌는 이동평균법(2025년 기준)" 형태 문장
"""
import json

import pytest
from fastapi.testclient import TestClient

from app import main, method_store, repo
from app.judge.rule_judge import RuleJudge
from app.llm.base import Completion, ToolCall

TEXT = "미래에셋에서 산 테슬라를 나무에서 팔았는데 두 계산서가 달라요"


class FakeLLM:
    name = "fake"

    def __init__(self):
        self.i = 0

    async def complete(self, messages, tools):
        self.i += 1
        if self.i == 1: return Completion(tool_call=ToolCall(id="1", name="sql_query", args={"intent": "전체"}))
        if self.i == 2: return Completion(tool_call=ToolCall(id="2", name="tax_calc", args={}))
        if self.i == 3: return Completion(text="상담원 메모: 이체 lot 포함.")
        return Completion(text="옮겨 온 주식은 원래 매수한 증권사의 취득가액이 적용됩니다.")


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("TRADES_FILE", str(repo.DATA / "samples" / "trades.json"))
    monkeypatch.setattr(main, "_judge", lambda: RuleJudge())
    monkeypatch.setattr(main, "_llm", lambda: FakeLLM())
    return TestClient(main.app)


def ask(client, **kw):
    return client.post("/v1/inquiries", json={"customer_id": 1077, "tax_year": 2025, "text": TEXT, **kw}).json()


def save(client, method, acc="NAMUH-1077", year=2025, who="agent01"):
    return client.post(f"/v1/customers/1077/accounts/{acc}/method", json={"tax_year": year, "method": method, "confirmed_by": who})


def test_1_no_record_unconfirmed_fifo(client):
    j = ask(client)
    assert j["status"] == "REVIEW" and j["review_reason"] == "METHOD_UNCONFIRMED"
    assert j["method"]["needs_confirmation"] == ["NAMUH-1077"] and j["method"]["confirmed"] == {}
    assert {a["method"] for a in j["figures"]["by_account"]} == {"FIFO"}
    assert "방식 저장" in j["agent_note"]


def test_2_saved_avg_then_auto_calc_with_avg(client):
    fifo = ask(client)["figures"]
    r = save(client, "AVG")
    assert r.status_code == 200 and r.json()["changed"] is True
    j = ask(client)
    assert j["method"]["needs_confirmation"] == [] and j["method"]["confirmed"] == {"NAMUH-1077": "AVG"}
    assert {a["method"] for a in j["figures"]["by_account"]} == {"AVG"}
    assert j["review_reason"] != "METHOD_UNCONFIRMED"  # 이체 lot이 있어 TRANSFER_REVIEW는 남는다
    # 4. 초안 문장 — 코드가 넣고, 고객 문장 규칙(영문 약어 없음)을 지킨다
    assert "NH투자증권(나무) 계좌는 고객님께서 선택하신 평균 매수 단가로 보는 방식(이동평균법)으로 계산했습니다(2025년 기준)." in j["draft"]
    assert "먼저 산 주식부터 판 것으로 보면" not in j["draft"]  # 확정됐으니 '가정' 문구 없음
    assert "AVG" not in j["draft"] and "저장된 방식: NAMUH-1077 AVG" in j["agent_note"]
    assert j["verify_failures"] == []


def test_3_second_change_rejected_same_value_idempotent(client):
    assert save(client, "AVG").status_code == 200
    r = save(client, "FIFO")
    assert r.status_code == 409 and r.json()["error"] == "E-METHOD-CHANGE"
    again = save(client, "AVG")
    assert again.status_code == 200 and again.json()["changed"] is False
    assert save(client, "FIFO", year=2026).status_code == 200  # 연도가 다르면 별개


def test_free_rule_allows_change(client, monkeypatch):
    monkeypatch.setenv("METHOD_CHANGE_RULE", "free")
    assert save(client, "AVG").status_code == 200
    assert save(client, "FIFO").status_code == 200


def test_non_choice_and_unknown_accounts(client):
    r = save(client, "AVG", acc="MIRAE-1077")
    assert r.status_code == 422 and r.json()["error"] == "E-METHOD-NOT-CHOICE"
    assert save(client, "AVG", acc="KB-9999").status_code == 404


def test_override_beats_saved_and_is_not_stored(client):
    save(client, "AVG")
    j = ask(client, method_override="FIFO")
    assert {a["method"] for a in j["figures"]["by_account"]} == {"FIFO"}
    assert "선택하신" not in j["draft"] and "method_override=FIFO 1회 적용" in j["agent_note"]
    assert method_store.get_store().get(1077, 2025)["NAMUH-1077"]["method"] == "AVG"  # 저장값은 그대로


def test_broker_method_tool_has_no_advice_fields(client):
    save(client, "AVG")
    out = client.post("/v1/tools/broker_method", json={"customer_id": 1077, "tax_year": 2025}).json()
    assert out["confirmed"]["NAMUH-1077"]["method"] == "AVG" and out["needs_confirmation"] == []
    blob = json.dumps(out, ensure_ascii=False)
    for w in ("유리", "불리", "추천", "better", "recommend"):  # F11-3
        assert w not in blob
