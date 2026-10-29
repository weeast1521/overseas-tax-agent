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



def test_duplicate_call_reuses_result_then_third_is_loop():
    """같은 호출 반복 1회째는 재실행 없이 직전 결과 재사용(E-DUP-CALL), 3회째 E-LOOP (10/29)."""
    import asyncio
    from app.agent import react
    from app.judge.rule_judge import RuleJudge
    from app.tools import registry
    from app.llm.base import Completion, ToolCall

    call = lambda i: Completion(text=None, tool_call=ToolCall(id=str(i), name="sql_query", args={"intent": "전체"}))  # noqa: E731
    script = [call(1), call(2), Completion(text="끝")]

    class Scripted:
        name = "ollama"

        async def complete(self, messages, tools):
            return script.pop(0)

    j = asyncio.run(RuleJudge().classify("세금 얼마예요"))
    res = asyncio.run(react.run("세금 얼마예요", j, Scripted(), registry.ToolContext(1042, 2025)))
    assert res.final == "끝" and [s.error_code for s in res.steps] == [None, "E-DUP-CALL", None]

    script[:] = [call(1), call(2), call(3)]
    res2 = asyncio.run(react.run("세금 얼마예요", j, Scripted(), registry.ToolContext(1042, 2025)))
    assert res2.aborted == "E-LOOP"


def test_sql_query_observation_carries_code_calculated_tax_summary():
    """결정 A (10/29): 세액 요약은 sql_query 응답의 tax_calc 블록으로 — 에이전트 툴 목록에 tax_calc 가 없다."""
    import asyncio
    from app.tools import registry
    from app import main

    assert "tax_calc" not in registry.AGENT_TOOLS and [t.name for t in registry.specs()] == ["sql_query", "reg_search"]
    ctx = registry.ToolContext(1042, 2025)
    ctx.figures, ctx.calc_error = main.compute_figures(1042, 2025, {})
    out = asyncio.run(registry.invoke("sql_query", {"intent": "전체 거래"}, ctx))
    assert out["tax_calc"]["tax_krw"] == ctx.figures["tax_krw"] and out["tax_calc"]["by_account"][0]["account_id"]
    assert list(out).index("tax_calc") < list(out).index("rows")  # 요약이 rows 보다 앞 (Observation 2,000자 잘림 대비)
    ctx2 = registry.ToolContext(1042, 2025)  # figures 없음 → 숫자 말하지 말라는 표식
    out2 = asyncio.run(registry.invoke("sql_query", {"intent": "전체 거래"}, ctx2))
    assert out2["tax_calc"]["error"] == "E-CALC-NONE"
