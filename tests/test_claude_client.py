"""ClaudeClient — payload 변환과 데이터 경계. API 호출 없음."""
import pytest

from app.llm.base import Message, ToolSpec
from app.llm.claude_client import ClaudeClient, DataBoundaryError, assert_synthetic_data


def test_rejects_real_data_file(monkeypatch):
    monkeypatch.setenv("TRADES_FILE", "data/mine/trades.json")
    with pytest.raises(DataBoundaryError):
        assert_synthetic_data()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    with pytest.raises(DataBoundaryError):
        ClaudeClient()


def test_allows_samples(monkeypatch):
    monkeypatch.setenv("TRADES_FILE", "data/samples/trades.json")
    assert_synthetic_data()
    monkeypatch.delenv("TRADES_FILE")
    assert_synthetic_data()


def test_payload_shape_and_tool_turns():
    spec = ToolSpec(name="sql_query", description="d", input_schema={"type": "object", "properties": {}})
    msgs = [
        Message(role="system", content="S1"), Message(role="system", content="S2"),
        Message(role="user", content="민원"),
        Message(role="assistant", content='{"intent": "x"}', tool_call_id="t1", name="sql_query"),
        Message(role="tool", content='{"rows": []}', tool_call_id="t1", name="sql_query"),
        Message(role="user", content="툴을 호출하거나 최종 답변 JSON을 써라."),
    ]
    req = ClaudeClient.to_anthropic(msgs, [spec])
    assert req["system"] == "S1\n\nS2"
    assert req["tools"] == [{"name": "sql_query", "description": "d", "input_schema": spec.input_schema}]
    assert [m["role"] for m in req["messages"]] == ["user", "assistant", "user", "user"]
    assert req["messages"][1]["content"][0] == {"type": "tool_use", "id": "t1", "name": "sql_query", "input": {"intent": "x"}}
    assert req["messages"][2]["content"][0]["type"] == "tool_result"
    assert "temperature" not in req


def test_default_model_and_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.delenv("CLAUDE_MODEL", raising=False)
    assert ClaudeClient().model == "claude-opus-5-5"
    monkeypatch.setenv("CLAUDE_MODEL", "claude-sonnet-5-5")
    monkeypatch.setenv("CLAUDE_MAX_TOKENS", "800")
    c = ClaudeClient()
    assert c.model == "claude-sonnet-5-5" and c.max_tokens == 800
