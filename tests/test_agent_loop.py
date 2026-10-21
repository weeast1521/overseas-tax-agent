"""F03 루프 규칙 테스트 — 가짜 LLM으로 E-LOOP·E-MAXSTEP·정상 종료 확인."""
import asyncio
import json

from app.agent import react
from app.judge.base import Judgment
from app.llm.base import Completion, ToolCall
from app.tools import registry


class FakeLLM:
    name = "fake"

    def __init__(self, script):
        self.script = list(script)

    async def complete(self, messages, tools):
        return self.script.pop(0) if self.script else Completion(text="끝")


def J():
    return Judgment(category="TAX_BASIS", confidence=0.98, required_tools=["sql_query", "tax_calc", "reg_search"])


def test_happy_path_sql_then_calc_then_final():
    llm = FakeLLM([
        Completion(tool_call=ToolCall(id="1", name="sql_query", args={"intent": "TSLA 2025"})),
        Completion(tool_call=ToolCall(id="2", name="tax_calc", args={})),
        Completion(text=json.dumps({"summary": "ok", "figures": {}, "sources": []})),
    ])
    r = asyncio.run(react.run("세금 얼마예요", J(), llm, registry.ToolContext(1023, 2025)))
    assert r.final and r.aborted is None
    assert [s.tool for s in r.steps] == ["sql_query", "tax_calc", None]
    assert '"tax_krw"' in r.steps[1].observation


def test_tax_calc_before_sql_returns_tool_order_error():
    llm = FakeLLM([Completion(tool_call=ToolCall(id="1", name="tax_calc", args={})), Completion(text="끝")])
    r = asyncio.run(react.run("x", J(), llm, registry.ToolContext(1023, 2025)))
    assert r.steps[0].error_code == "E-TOOL-ORDER"


def test_loop_detection():
    same = Completion(tool_call=ToolCall(id="1", name="sql_query", args={"intent": "TSLA"}))
    r = asyncio.run(react.run("x", J(), FakeLLM([same, same, same]), registry.ToolContext(1023, 2025)))
    assert r.aborted == "E-LOOP"


def test_max_steps():
    calls = [Completion(tool_call=ToolCall(id=str(i), name="reg_search", args={"query": f"q{i}"})) for i in range(20)]
    r = asyncio.run(react.run("x", J(), FakeLLM(calls), registry.ToolContext(1023, 2025)))
    assert r.aborted == "E-MAXSTEP" and len(r.steps) == react.MAX_STEPS
