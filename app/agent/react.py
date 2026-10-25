"""F03 ReAct 루프 — 프레임워크 없이 직접 구현.

LLM은 다음 행동(툴 호출 또는 최종 답변)만 고른다. 숫자·사실은 Observation에서 온다.
- MAX_STEPS 8 → E-MAXSTEP
- 같은 툴·같은 인자 2회 → E-LOOP
- 같은 툴 2회 연속 실패 → E-TOOL-FAIL
- 포맷 오류 2회 → E-LLM-FORMAT
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field

from app.judge.base import Judgment
from app.llm.base import Completion, LLMClient, Message
from app.tools import registry

MAX_STEPS = 8

# 최종 답변 형식. json = 조립기(F07)에 넘길 재료 / prose = 프롬프트만으로 고객용 문장 (대조 실험용, AGENT_FINAL_FORMAT=prose)
FINAL_FORMATS = {
    "json": """최종 답변은 JSON 한 개:
   {"summary": "<상담원용 요약 3문장 이내>", "figures": {<tax_calc에서 그대로 가져온 숫자>}, "sources": [{"law":..., "article":...}]}""",
    "prose": """최종 답변은 상담원이 고객에게 전화로 그대로 읽어 줄 한국어 문장이다. 규칙:
   - 존댓말, 핵심 3문장 이내. 전문용어(FIFO, lot, CHOICE 등)는 쓰지 말고 풀어서 ("먼저 산 주식부터 판 것으로 계산")
   - JSON, 중괄호, 영문 키, 계좌 ID, 거래번호, 툴 이름, "확인 필요"·"안내해 주세요" 같은 상담원·내부용 문장을 절대 넣지 않는다
   - 금액은 tax_calc 출력 그대로 원 단위로 쓴다. "약"·반올림 금지. 어느 방식이 유리한지 말하지 않는다
   - 마지막 줄에 "근거: <법 이름> <조문>" 한 줄 (reg_search로 찾은 것만)""",
}

SYSTEM_PROMPT = """너는 증권사 고객센터의 해외주식 양도소득세 상담 보조 에이전트다. 상담원이 넣은 고객 민원에 대해
근거 있는 답변 초안의 재료를 모은다.

규칙:
1. 고객의 실제 거래는 sql_query로만 안다. 추측하지 않는다.
2. 세액·양도차익·취득가액 등 모든 숫자는 tax_calc 출력만 쓴다. 네가 계산하지 않는다.
3. 규정 설명에는 reg_search로 찾은 조문을 붙인다. 찾지 못한 규정은 말하지 않는다.
   순서: 거래·세액이 걸린 민원은 sql_query → tax_calc를 먼저 끝내고, reg_search는 설명에 필요한 조문을 위해 1회만 부른다.
   검색 결과가 빈약해도 같은 툴을 다시 부르지 말고(쿼리 재작성은 툴 안에서 한다) 찾은 것만으로 답한다. 응답 시간 제한이 30초다.
4. 필요한 정보가 모이면 툴 호출 없이 최종 답변을 쓴다. {final_format}
민원 유형: {category} (분류 확신도 {confidence}). 보통 필요한 툴: {tools}."""


@dataclass
class Step:
    step_no: int
    thought: str | None
    tool: str | None
    args: dict | None
    observation: str | None
    latency_ms: int
    in_tokens: int = 0
    out_tokens: int = 0
    error_code: str | None = None
    off_plan: bool = False


@dataclass
class AgentResult:
    final: str | None = None
    aborted: str | None = None
    steps: list[Step] = field(default_factory=list)


def _sig(name: str, args: dict) -> str:
    norm = json.dumps(args, sort_keys=True, ensure_ascii=False).lower().replace(" ", "")
    return hashlib.sha1(f"{name}:{norm}".encode()).hexdigest()


async def run(inquiry: str, judgment: Judgment, llm: LLMClient, ctx: registry.ToolContext) -> AgentResult:
    res = AgentResult()
    tools = registry.specs()  # 필요 툴 외 호출도 허용, off_plan으로 표시 (F03-6)
    history = [
        Message(role="system", content=SYSTEM_PROMPT.replace("{final_format}", FINAL_FORMATS[os.getenv("AGENT_FINAL_FORMAT", "json")])
                .replace("{category}", judgment.category)
                .replace("{tools}", ", ".join(judgment.required_tools) or "없음")
                .replace("{confidence}", f"{judgment.confidence:.2f}")),
        Message(role="user", content=f"고객 ID {ctx.customer_id}, 과세연도 {ctx.tax_year}.\n민원: {inquiry}"),
    ]
    seen: dict[str, int] = {}
    fails: dict[str, int] = {}
    format_errors = 0

    for step_no in range(MAX_STEPS):
        t0 = time.perf_counter()
        reply: Completion = await llm.complete(history, tools)
        lat = int((time.perf_counter() - t0) * 1000)

        if reply.final:
            res.steps.append(Step(step_no, reply.text, None, None, None, lat, reply.in_tokens, reply.out_tokens))
            res.final = reply.text
            return res

        if reply.tool_call is None:
            format_errors += 1
            res.steps.append(Step(step_no, reply.text, None, None, None, lat, reply.in_tokens, reply.out_tokens, "E-LLM-FORMAT"))
            if format_errors >= 2:
                res.aborted = "E-LLM-FORMAT"
                return res
            history.append(Message(role="user", content="툴을 호출하거나 최종 답변 JSON을 써라."))
            continue

        call = reply.tool_call
        sig = _sig(call.name, call.args)
        seen[sig] = seen.get(sig, 0) + 1
        if seen[sig] >= 2:
            res.steps.append(Step(step_no, reply.text, call.name, call.args, None, lat, error_code="E-LOOP"))
            res.aborted = "E-LOOP"
            return res

        obs = await registry.invoke(call.name, call.args, ctx)
        obs_text = json.dumps(obs, ensure_ascii=False, default=str)[:2000]
        err = obs.get("error")
        fails[call.name] = fails.get(call.name, 0) + 1 if err else 0
        res.steps.append(Step(step_no, reply.text, call.name, call.args, obs_text, lat, reply.in_tokens, reply.out_tokens,
                              err, off_plan=call.name not in judgment.required_tools))
        if fails[call.name] >= 2:
            res.aborted = "E-TOOL-FAIL"
            return res

        history.append(Message(role="assistant", content=json.dumps(call.args, ensure_ascii=False), tool_call_id=call.id, name=call.name))
        history.append(Message(role="tool", content=obs_text, tool_call_id=call.id, name=call.name))

    res.aborted = "E-MAXSTEP"
    return res
