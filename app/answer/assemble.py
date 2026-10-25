"""F07 답변 조립기 (SCRUM-35).

assemble(...) → Draft(customer_text, agent_note, sources, facts)

역할 분담 (절제 실험 결론: "문장은 프롬프트, 상태·인용·숫자는 코드"):
  LLM  — 골격 순서대로 '설명' 문장만 쓴다. 숫자는 주어진 문자열만, 조문은 주어진 토큰만, 상태 문장은 쓰지 않는다.
  코드 — 숫자 문장, 상태 문장(방식 미확정·이체 검토·매수내역 부족), "근거:" 줄을 끼워 넣고, 고객용 문장과 상담원 메모를 분리한다.
LLM 호출이 실패하거나 llm=None이면 코드 부분만으로 초안을 만든다 (상담원은 숫자·상태는 항상 받는다).
사후 검증(숫자 완전 일치·출처 존재·금지 표현·길이)은 verify.py(SCRUM-36)가 한다. 여기서는 만들기만.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from app.answer import skeleton as sk
from app.llm.base import Completion, LLMClient, Message

SYSTEM = """너는 증권사 고객센터 상담원이 고객에게 전화로 그대로 읽어 줄 안내문의 '설명 부분'을 쓴다.

규칙:
1. 아래 '설명 순서'대로, 존댓말로, {max_len}자 이내, 3문장 이내로 쓴다. 자연스러운 단락 하나로, 줄바꿈 없이.
   '[숫자]' 항목은 건너뛴다 — 금액 문장은 시스템이 뒤에 붙인다.
2. 금액·수량·비율 숫자를 하나도 쓰지 않는다. 숫자는 시스템이 붙인다. (연도는 써도 된다)
3. 법 이름·조문 번호·대괄호 인용을 쓰지 않는다. 근거 조문은 시스템이 따로 표시한다.
   설명 내용은 '참고 규정 내용'과 맞아야 하고, 거기에 없는 규정은 단정하지 않는다. 규정 내용이 없으면 일반적인 절차만 말한다.
4. '시스템이 덧붙일 상태 문장'의 내용(방식 확인 필요, 이체 검토, 매수내역 부족)은 네가 쓰지 않는다. 같은 말을 반복하지 않는다.
5. 금지: 투자 권유, 어느 산정 방식이 유리·불리하다는 판단, "반드시"·"무조건" 같은 단정, 고객 이름·계좌번호·거래번호,
   JSON·중괄호·영문 키·툴 이름, "확인 필요"·"안내해 주세요" 같은 상담원·내부용 문장. 전문용어(FIFO, lot, CHOICE)는 풀어 쓴다.
6. 설명 문장만 출력한다. 제목·머리말·마크다운·인사말 없이."""

USER = """민원 유형: {category}
민원: {inquiry}

설명 순서:
{skeleton}

시스템이 덧붙일 금액 문장: {numbers}
참고 규정 내용(검색 결과): {sources}
시스템이 덧붙일 상태 문장: {status}
에이전트가 모은 참고 요약(내부용, 그대로 옮기지 말 것): {summary}"""


@dataclass
class Draft:
    customer_text: str  # 상담원이 고객에게 읽어 주는 문장
    agent_note: str  # 상담원만 보는 메모 (상태 사유·확인할 계좌·내부 오류)
    sources: list[dict]  # reg_search로 받은 (law, article)만
    facts: dict = field(default_factory=dict)  # 조립에 쓴 사실 — verify(SCRUM-36)의 정답 집합
    llm_text: str | None = None  # LLM이 쓴 설명 부분 원문 (검증·디버그)
    tokens_in: int = 0
    tokens_out: int = 0
    error: str | None = None  # LLM 실패 코드. 코드 부분만으로 조립됐음을 뜻한다


def _source_set(chunks: list[dict]) -> list[dict]:
    seen, out = set(), []
    for c in chunks or []:
        key = (c.get("law"), c.get("article"))
        if key[0] and key[1] and key not in seen:
            seen.add(key)
            out.append({"law": c["law"], "article": c["article"], "chunk_id": c.get("chunk_id"),
                        "_text": re.sub(r"★[^★]*★\s*", "", str(c.get("text") or ""))[:180]})
    return out


def _summary_of(agent_final: str | None) -> str | None:
    """에이전트 최종 출력에서 요약만 뽑는다 (JSON 모드 summary, 문장 모드 본문)."""
    if not agent_final:
        return None
    m = re.search(r"\{.*\}", agent_final, re.S)
    if m:
        try:
            j = json.loads(m.group(0))
            return str(j.get("summary") or "")[:600] or None
        except Exception:
            pass
    return agent_final[:600]


def build_facts(category: str, figures: dict | None, chunks: list[dict], needs_confirmation: list[str],
                calc_error: str | None, aborted: str | None, tax_year: int) -> dict:
    """코드가 보장하는 사실 묶음. LLM 입력이자 verify의 정답."""
    figures = figures or {}
    numbers: dict[str, str] = {}
    for k in ("total_gain_krw", "deduction_krw", "taxable_krw", "tax_krw"):
        if figures.get(k) is not None:
            numbers[k] = sk.won(figures[k])
    for a in figures.get("by_account") or []:
        numbers[f"gain_{a['account_id']}"] = f"{sk.BROKER_KO.get(a['broker_code'], a['broker_code'])} {sk.won(a['gain_krw'])}"
    status: list[str] = []
    if needs_confirmation:
        brokers = sorted({sk.BROKER_KO.get(acc.split("-")[0], acc.split("-")[0]) for acc in needs_confirmation})
        status.append(sk.STATUS_SENTENCES["METHOD_UNCONFIRMED"].format(broker="·".join(brokers)))
    if figures.get("transferred_lot_sales"):
        origin, dest = sk.transfer_pairs(figures)
        status.append(sk.STATUS_SENTENCES["TRANSFER_REVIEW"].format(origin=origin, dest=dest))
    if calc_error == "E-CALC-SHORT":
        status.append(sk.STATUS_SENTENCES["SHORT"])
    if aborted:
        status.append(sk.STATUS_SENTENCES["AGENT_ABORTED"])
    return {
        "category": category, "tax_year": tax_year, "numbers": numbers,
        "numbers_sentence": sk.numbers_sentence(figures, tax_year), "by_account_sentence": sk.by_account_sentence(figures),
        "status_sentences": status, "sources": _source_set(chunks),
        "skeleton": sk.SKELETONS.get(category, sk.DEFAULT_SKELETON),
    }


def _clean(text: str) -> str:
    """LLM 설명에서 기계 흔적을 걷어낸다. 내용은 바꾸지 않는다."""
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    if text.strip().startswith("{"):  # 지시를 어기고 JSON을 낸 경우 summary만 살린다
        text = _summary_of(text) or ""
        if text.strip().startswith("{"):
            return ""
    text = re.sub(r"^\s*(설명|답변|안내)\s*[:：]\s*", "", text.strip())
    text = re.sub(r"[*_#`]+", "", text)
    text = re.sub(r"\s*\[[^\[\]]*제\s*\d+\s*조[^\[\]]*\]", "", text)  # 지시를 어긴 [법 조문] 토큰 — 검증은 원문(llm_text)으로 따로 한다
    return re.sub(r"\s*\n\s*", " ", text).strip()


def _fit(text: str, limit: int) -> str:
    """문장 끝에서 자른다. 중간에 끊긴 문장은 버린다 (조립기 v1 가독성 감점 원인)."""
    cut = text[:limit].rstrip()
    if cut.endswith(("다.", "요.", "다", "요")) and len(text) <= limit:
        return cut
    idx = max(cut.rfind("다."), cut.rfind("요."))
    return cut[: idx + 2].strip() if idx > 0 else ""


def code_part(facts: dict) -> str:
    parts = [facts.get("by_account_sentence"), facts.get("numbers_sentence"), *(facts.get("status_sentences") or [])]
    return " ".join(p for p in parts if p)


def llm_budget(facts: dict) -> int:
    """LLM 설명 몫 = 600 - 코드 문장 길이 - 여유. 최대 MAX_LLM_PART."""
    return max(0, min(sk.MAX_LLM_PART, sk.MAX_CUSTOMER_TEXT - len(code_part(facts)) - 5))


def compose(facts: dict, llm_text: str | None) -> str:
    """고객용 문장 = LLM 설명 + 계좌별 숫자 + 숫자 문장 + 상태 문장. 순서·존재·길이를 코드가 보장한다.
    근거 조문은 고객 문장에 넣지 않는다(전화로 읽기 어려움, v1 채점 감점) → Draft.sources와 상담원 메모로."""
    head = _fit(_clean(llm_text), llm_budget(facts)) if llm_text else ""
    return " ".join(p for p in (head, code_part(facts)) if p).strip()


def agent_note_of(facts: dict, needs_confirmation: list[str], figures: dict | None, calc_error: str | None,
                  aborted: str | None, summary: str | None, llm_error: str | None) -> str:
    lines: list[str] = []
    if needs_confirmation:
        lines.append(f"방식 미확정 계좌: {', '.join(needs_confirmation)} — 고객과 FIFO/이동평균 확인 후 method_override로 재실행")
    tls = (figures or {}).get("transferred_lot_sales") or []
    if tls:
        lines.append(f"이체 lot 포함 매도 {len(tls)}건: {', '.join(tls[:5])}{' …' if len(tls) > 5 else ''} — 원 매수 증권사 취득가액 적용됨, 계산서와 차이 설명 필요")
    if calc_error:
        lines.append(f"계산 오류 {calc_error} — 거래 누락 가능성, 숫자 안내 보류")
    if aborted:
        lines.append(f"에이전트 중단 {aborted} — 규정 설명은 에이전트 없이 조립됨")
    if llm_error:
        lines.append(f"조립 LLM 실패 {llm_error} — 설명 부분 없이 코드 문장만 내보냄")
    if facts.get("sources"):
        lines.append("근거: " + ", ".join(f"{s['law']} {s['article']}" for s in facts["sources"]))
    else:
        lines.append("인용 조문 없음 — 규정 설명은 조문 없이 절차만")
    if summary:
        lines.append(f"에이전트 요약: {summary}")
    return "\n".join(lines)


async def assemble(category: str, inquiry: str, figures: dict | None, chunks: list[dict], needs_confirmation: list[str],
                   *, llm: LLMClient | None, agent_final: str | None = None, calc_error: str | None = None,
                   aborted: str | None = None, tax_year: int = 2025) -> Draft:
    facts = build_facts(category, figures, chunks, needs_confirmation, calc_error, aborted, tax_year)
    summary = _summary_of(agent_final)
    llm_text, tin, tout, err = None, 0, 0, None
    if llm is not None and llm_budget(facts) >= 40:
        msgs = [
            Message(role="system", content=SYSTEM.replace("{max_len}", str(llm_budget(facts)))),
            Message(role="user", content=USER.format(
                category=category, inquiry=inquiry[:500],
                skeleton="\n".join(f"{i}. {s}" for i, s in enumerate(facts["skeleton"], 1)),
                numbers=code_part(facts) or "(없음)",
                sources=" / ".join(f"{s['law']} {s['article']}: {s['_text']}" for s in facts["sources"]) or "(없음 — 규정을 단정하지 말 것)",
                status=" / ".join(facts["status_sentences"]) or "(없음)",
                summary=summary or "(없음)")),
        ]
        try:
            rep: Completion = await llm.complete(msgs, [])
            tin, tout = rep.in_tokens, rep.out_tokens
            if rep.text and rep.text.strip():
                llm_text = rep.text
            else:
                err = "E-ASSEMBLE-EMPTY"
        except Exception as e:  # 조립 LLM이 죽어도 숫자·상태 문장은 나간다
            err = f"E-ASSEMBLE:{type(e).__name__}"
    text = compose(facts, llm_text)
    return Draft(customer_text=text,
                 agent_note=agent_note_of(facts, needs_confirmation, figures, calc_error, aborted, summary, err),
                 sources=[{k: v for k, v in x.items() if not k.startswith("_")} for x in facts["sources"]], facts=facts, llm_text=llm_text, tokens_in=tin, tokens_out=tout, error=err)
