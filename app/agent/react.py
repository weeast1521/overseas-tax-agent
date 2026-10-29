"""F03 ReAct 루프 — 프레임워크 없이 직접 구현.

LLM은 다음 행동(툴 호출 또는 최종 답변)만 고른다. 숫자·사실은 Observation에서 온다.
- MAX_STEPS 8 → E-MAXSTEP
- 같은 툴·같은 인자 2회 → E-LOOP
- 같은 툴 2회 연속 실패 → E-TOOL-FAIL
- 포맷 오류 2회 → E-LLM-FORMAT
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from dataclasses import dataclass, field

from app.judge.base import Judgment
from app.llm.base import Completion, LLMClient, Message
from app.tools import registry

MAX_STEPS = 8

# 최종 답변 형식 (AGENT_FINAL_FORMAT).
#   notes (기본) = 상담원용 짧은 메모. 고객 문장·숫자는 조립기(F07)가 코드로 만든다 — 에이전트가 숫자를 다시 쓸 이유가 없다.
#                 SCRUM-19: json 모드는 최종 답에 figures를 통째로 다시 써서 출력 토큰이 많고 30초를 자주 넘겼다.
#   json  = 조립기 이전의 기준 형식 (before 비교용) / prose = 프롬프트만으로 고객용 문장 (절제 실험용)
FINAL_FORMATS = {
    "notes": """최종 답변은 상담원용 메모다. 한국어 3줄 이내, 숫자·금액·JSON·고객용 문장은 쓰지 않는다(시스템이 계산 결과로 고객 안내문을 조립한다).
   쓸 것: 민원의 핵심 쟁점 한 줄, 확인한 사실(어느 증권사·이체 여부·손익통산 여부 등) 한 줄, 상담원이 고객에게 확인할 점 한 줄.""",
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
2. 세액·양도차익·취득가액 등 모든 숫자는 sql_query 응답의 tax_calc 블록(코드가 계산한 것)에서만 쓴다. 네가 계산하지 않는다.
3. 규정 설명에는 reg_search로 찾은 조문을 붙인다. 찾지 못한 규정은 말하지 않는다.
   순서: 거래·세액이 걸린 민원은 sql_query 를 먼저, reg_search는 설명에 필요한 조문을 위해 1회만 부른다.
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
    tool_ms: int = 0  # 툴 실행 시간 (latency_ms 는 LLM 응답 시간)


@dataclass
class AgentResult:
    final: str | None = None
    aborted: str | None = None
    steps: list[Step] = field(default_factory=list)
    error_detail: str | None = None


def _sig(name: str, args: dict) -> str:
    norm = json.dumps(args, sort_keys=True, ensure_ascii=False).lower().replace(" ", "")
    return hashlib.sha1(f"{name}:{norm}".encode()).hexdigest()


async def run(inquiry: str, judgment: Judgment, llm: LLMClient, ctx: registry.ToolContext,
              res: AgentResult | None = None) -> AgentResult:
    """res를 넘기면 스텝을 그 객체에 쌓는다 — 호출자가 타임아웃으로 취소해도 그때까지의 스텝이 남는다 (SCRUM-19)."""
    res = res if res is not None else AgentResult()
    tools = registry.specs()  # 필요 툴 외 호출도 허용, off_plan으로 표시 (F03-6)
    history = [
        Message(role="system", content=SYSTEM_PROMPT.replace("{final_format}", FINAL_FORMATS[os.getenv("AGENT_FINAL_FORMAT", "notes")])
                .replace("{category}", judgment.category)
                .replace("{tools}", ", ".join(judgment.required_tools) or "없음")
                .replace("{confidence}", f"{judgment.confidence:.2f}")),
        Message(role="user", content=f"고객 ID {ctx.customer_id}, 과세연도 {ctx.tax_year}.\n민원: {inquiry}"),
    ]
    seen: dict[str, int] = {}
    fails: dict[str, int] = {}
    format_errors = 0
    called: set[str] = set()
    last_obs: dict[str, str] = {}

    for step_no in range(MAX_STEPS):
        t0 = time.perf_counter()
        try:
            reply: Completion = await llm.complete(history, tools)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # LLM 호출 실패(로컬 Ollama 다운·API 오류). 다른 제공자로 폴백하지 않는다 — 폐쇄망 원칙 (SCRUM-51)
            res.error_detail = f"{type(e).__name__}: {str(e)[:200]}"
            res.steps.append(Step(step_no, None, None, None, res.error_detail, int((time.perf_counter() - t0) * 1000), error_code="E-LLM"))  # 원인은 observation 에 남긴다
            res.aborted = "E-LLM"
            return res
        lat = int((time.perf_counter() - t0) * 1000)

        if reply.final:
            # (10/29) 한때 tax_calc 를 빼먹으면 되묻는 '계획 강제'가 있었다 — 로컬 7B 가 되묻기를 "다시 조회"로 읽어 E-LOOP 11/30. 결정 A 로 tax_calc 자체를
            # 에이전트 툴에서 빼고 계산 요약을 sql_query 응답에 넣으면서 되묻기는 제거했다.
            res.steps.append(Step(step_no, reply.text, None, None, None, lat, reply.in_tokens, reply.out_tokens))
            res.final = reply.text
            return res

        if reply.tool_call is None:
            format_errors += 1
            res.steps.append(Step(step_no, reply.text, None, None, None, lat, reply.in_tokens, reply.out_tokens, "E-LLM-FORMAT"))
            if format_errors >= 2:
                res.aborted = "E-LLM-FORMAT"
                return res
            history.append(Message(role="user", content="툴을 호출하거나 최종 답변을 써라."))
            continue

        call = reply.tool_call
        sig = _sig(call.name, call.args)
        seen[sig] = seen.get(sig, 0) + 1
        if seen[sig] == 2 and sig in last_obs:
            # 같은 호출 반복 1회째: 재실행하지 않고 직전 결과를 돌려주며 안내한다 (작은 모델이 자주 한다). 3회째부터 E-LOOP.
            res.steps.append(Step(step_no, reply.text, call.name, call.args, "(중복 호출 — 직전 결과 재사용)", lat, reply.in_tokens, reply.out_tokens, error_code="E-DUP-CALL"))
            history.append(Message(role="assistant", content=json.dumps(call.args, ensure_ascii=False), tool_call_id=call.id, name=call.name))
            history.append(Message(role="tool", content=last_obs[sig], tool_call_id=call.id, name=call.name))
            history.append(Message(role="user", content=f"{call.name} 은(는) 이미 같은 결과다. 다른 툴을 호출하거나 최종 답변을 써라."))
            continue
        if seen[sig] >= 3:
            res.steps.append(Step(step_no, reply.text, call.name, call.args, None, lat, error_code="E-LOOP"))
            res.aborted = "E-LOOP"
            return res

        t1 = time.perf_counter()
        obs = await registry.invoke(call.name, call.args, ctx)
        tool_ms = int((time.perf_counter() - t1) * 1000)
        obs_text = json.dumps(obs, ensure_ascii=False, default=str)[:2000]
        err = obs.get("error")
        fails[call.name] = fails.get(call.name, 0) + 1 if err else 0
        res.steps.append(Step(step_no, reply.text, call.name, call.args, obs_text, lat, reply.in_tokens, reply.out_tokens,
                              err, off_plan=call.name not in judgment.required_tools, tool_ms=tool_ms))
        if fails[call.name] >= 2:
            res.aborted = "E-TOOL-FAIL"
            return res

        called.add(call.name)
        last_obs[sig] = obs_text
        history.append(Message(role="assistant", content=json.dumps(call.args, ensure_ascii=False), tool_call_id=call.id, name=call.name))
        history.append(Message(role="tool", content=obs_text, tool_call_id=call.id, name=call.name))

    res.aborted = "E-MAXSTEP"
    return res
