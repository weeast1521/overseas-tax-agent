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


def test_missing_tool_args_and_tool_exception_do_not_crash():
    """평가 1차(Haiku) 크래시 재현: reg_search를 인자 없이 호출 → E-TOOL-ARGS Observation, 루프 계속."""
    llm = FakeLLM([
        Completion(tool_call=ToolCall(id="1", name="reg_search", args={})),
        Completion(tool_call=ToolCall(id="2", name="reg_search", args={"query": "기본공제"})),
        Completion(text=json.dumps({"summary": "ok", "figures": {}, "sources": []})),
    ])
    r = asyncio.run(react.run("기본공제가 뭐예요", J(), llm, registry.ToolContext(1023, 2025)))
    assert r.final and r.aborted is None
    assert r.steps[0].error_code == "E-TOOL-ARGS" and r.steps[1].error_code is None


def test_tool_internal_exception_becomes_observation(monkeypatch):
    async def boom(args, ctx):
        raise RuntimeError("db down")
    monkeypatch.setitem(registry.TOOLS, "reg_search", boom)
    obs = asyncio.run(registry.invoke("reg_search", {"query": "x"}, registry.ToolContext(1023, 2025)))
    assert obs["error"] == "E-TOOL-EXC" and "db down" in obs["message"]


def test_final_without_tax_calc_is_nudged_once():
    """계획 강제 (SCRUM-45): tax_calc 를 건너뛰고 끝내면 1회 되묻고, 그래도 안 부르면 그대로 끝낸다 (E-PLAN-NUDGE 스텝 1개)."""
    import asyncio
    from app.agent import react
    from app.judge.rule_judge import RuleJudge
    from app.tools import registry

    class Skipper:
        name = "ollama"

        def __init__(self):
            self.n = 0

        async def complete(self, messages, tools):
            from app.llm.base import Completion, ToolCall
            self.n += 1
            if self.n == 1:
                return Completion(text=None, tool_call=ToolCall(id="1", name="sql_query", args={"intent": "전체"}))
            if self.n == 2:
                return Completion(text="메모: 계산 없이 끝")  # tax_calc 건너뜀 → 되묻기
            if self.n == 3:
                assert any("tax_calc" in m.content for m in messages if m.role == "user")
                return Completion(text=None, tool_call=ToolCall(id="2", name="tax_calc", args={}))
            return Completion(text="메모: 계산 반영")

    j = asyncio.run(RuleJudge().classify("세금 얼마예요"))
    res = asyncio.run(react.run("세금 얼마예요", j, Skipper(), registry.ToolContext(1042, 2025)))
    assert res.final == "메모: 계산 반영" and [s.tool for s in res.steps] == ["sql_query", None, "tax_calc", None]
    assert res.steps[1].error_code == "E-PLAN-NUDGE"
