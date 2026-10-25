"""F07-5~8 사후 검증 (SCRUM-36). 조립된 고객용 문장을 코드가 검사한다. 하나라도 걸리면 REVIEW/VERIFY_FAILED.

"LLM 출력을 믿지 않는다"를 코드로 보여 주는 지점. 검사:
  E-VERIFY-NUMBER  문장 속 금액·수량이 계산기(figures)에서 나온 값과 완전 일치하지 않음 ("약"·반올림·만 원 표기 포함)  (F07-5)
  E-VERIFY-SOURCE  조문 언급이 reg_search로 받은 집합 밖 — 모델이 기억으로 적은 조문 (10/25 절제 실험 20건 중 7건)  (F07-6)
  E-VERIFY-ADVICE  투자 권유·유리/불리 판단·단정 표현  (F07-7)
  E-VERIFY-LEAK    내부 지시문·툴 이름·JSON 흔적·전문 약어 (LLM-as-judge ③ 감점 사유 대부분)
  E-VERIFY-PII     계좌 ID·거래번호
  E-VERIFY-LENGTH  600자 초과  (F07-8)
  E-VERIFY-STATUS  코드가 넣어야 할 상태 문장이 빠짐 (조립기 불변식)
결정(10/21): 숫자는 완전 일치만, 반올림 허용 안 함.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.answer import skeleton as sk

_AMOUNT = re.compile(r"(?<![\d,.])(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?\s*(원|달러|주(?!식))")  # 소수점 포함 전체를 잡는다 (12.63달러의 63만 잡던 버그)
_WON_UNIT = re.compile(r"\d+(?:\.\d+)?\s*(만|억|천)\s*원")
_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_ARTICLE = re.compile(r"(제\s*\d+\s*조(?:\s*의\s*\d+)?)")
_LAW_TOKEN = re.compile(r"\[([^\[\]]+?)\s+(제\s*\d+\s*조(?:\s*의\s*\d+)?)[^\[\]]*\]")
_APPROX = re.compile(r"(약|대략|대충|얼추)\s*\d")
_ADVICE = re.compile(r"유리|불리|추천|권유|권해|사세요|파세요|매수하세요|매도하세요|바꾸세요|변경하세요|반드시|무조건|절대로")
_LEAK = re.compile(r"sql_query|tax_calc|reg_search|broker_method|FIFO|\bAVG\b|CHOICE|\blot\b|JSON|json|확인 필요|안내해 주세요|안내하세요|안내 바랍니다|상담원은|상담원이 안내|\{|\}|```")
_PII = re.compile(r"\b[A-Z]{2,}-\d{4}(?:-|\b)|\b[A-Z]{2,}-\d{8}-\d+\b")
_ALLOWED_PCT = {"22", "20", "2"}  # 22% = 양도소득세 20% + 지방소득세 2%


@dataclass
class VerifyResult:
    ok: bool
    failures: list[dict] = field(default_factory=list)  # {"code", "detail"}

    @property
    def codes(self) -> list[str]:
        return sorted({f["code"] for f in self.failures})


def _norm_art(a: str) -> str:
    return re.sub(r"\s+", "", a)


def allowed_numbers(figures: dict | None) -> set[str]:
    """계산기 결과에서 나올 수 있는 모든 숫자(콤마 없는 문자열). 금액·수량."""
    out: set[str] = set()
    if not figures:
        return out

    def add(v):
        if isinstance(v, bool) or v is None:
            return
        if isinstance(v, (int, float)):
            out.add(str(int(v)) if float(v).is_integer() else str(v))
        elif isinstance(v, str):
            try:
                f = float(v); out.add(str(int(f)) if f.is_integer() else str(f))
            except ValueError:
                pass

    for k in ("total_gain_krw", "deduction_krw", "taxable_krw", "tax_krw"):
        add(figures.get(k))
    for a in figures.get("by_account") or []:
        add(a.get("gain_krw"))
        for s in a.get("sales") or []:
            for k in ("proceeds_krw", "cost_krw", "gain_krw", "avg_cost_gain_krw", "sold_qty", "usd_gain"):
                add(s.get(k))
            for u in s.get("lots_used") or []:
                for k in ("qty", "cost_krw", "unit_price_usd"):
                    add(u.get(k))
    return out


def verify(text: str, figures: dict | None, sources: list[dict], status_sentences: list[str] | None = None,
           raw_llm: str | None = None) -> VerifyResult:
    """raw_llm: 조립기가 정리하기 전 LLM 원문. 조립기가 [조문] 토큰을 지워도 지어낸 인용은 여기서 잡는다."""
    fails: list[dict] = []
    body = text or ""
    # 근거 줄은 숫자·표현 검사에서 제외하고 조문 검사에만 쓴다
    main_part, _, src_line = body.partition("\n근거:")

    # 1. 숫자 완전 일치 (F07-5)
    allowed = allowed_numbers(figures)
    for m in _AMOUNT.finditer(main_part):
        f = float(m.group(1).replace(",", "") + (m.group(2) or ""))
        raw = str(int(f)) if f.is_integer() else str(f)
        if raw not in allowed and "-" + raw not in allowed:  # 손실은 음수로 들어 있다
            fails.append({"code": "E-VERIFY-NUMBER", "detail": f"{m.group(0).strip()} — 계산 결과에 없는 숫자"})
    for m in _WON_UNIT.finditer(main_part):
        fails.append({"code": "E-VERIFY-NUMBER", "detail": f"{m.group(0)} — 만/억 단위 표기(완전 일치 아님)"})
    for m in _APPROX.finditer(main_part):
        fails.append({"code": "E-VERIFY-NUMBER", "detail": f"'{m.group(0)}…' — 근사 표현"})
    for m in _PERCENT.finditer(main_part):
        if m.group(1) not in _ALLOWED_PCT:
            fails.append({"code": "E-VERIFY-NUMBER", "detail": f"{m.group(0)} — 허용되지 않은 비율"})

    # 2. 출처 ⊆ reg_search 집합 (F07-6)
    allowed_arts = {_norm_art(s["article"]) for s in sources or [] if s.get("article")}
    allowed_pairs = {(s["law"], _norm_art(s["article"])) for s in sources or [] if s.get("article")}
    reported: set[str] = set()
    cite_scope = body + ("\n" + raw_llm if raw_llm else "")
    for m in _LAW_TOKEN.finditer(cite_scope):
        if (m.group(1).strip(), _norm_art(m.group(2))) not in allowed_pairs:
            fails.append({"code": "E-VERIFY-SOURCE", "detail": f"[{m.group(1)} {m.group(2)}] — 검색 결과에 없는 조문"})
            reported.add(_norm_art(m.group(2)))
    for m in _ARTICLE.finditer(cite_scope):
        if _norm_art(m.group(1)) not in allowed_arts and _norm_art(m.group(1)) not in reported:
            reported.add(_norm_art(m.group(1)))
            fails.append({"code": "E-VERIFY-SOURCE", "detail": f"{m.group(1)} — 검색 결과에 없는 조문"})

    # 3. 금지 표현 (F07-7)
    for m in _ADVICE.finditer(main_part):
        fails.append({"code": "E-VERIFY-ADVICE", "detail": f"'{m.group(0)}'"})
    for m in _LEAK.finditer(main_part):
        fails.append({"code": "E-VERIFY-LEAK", "detail": f"'{m.group(0)}' — 내부용 표현·기계 흔적"})
    for m in _PII.finditer(main_part):
        fails.append({"code": "E-VERIFY-PII", "detail": f"'{m.group(0)}' — 계좌·거래 식별자"})

    # 4. 길이 (F07-8)
    if len(body) > sk.MAX_CUSTOMER_TEXT:
        fails.append({"code": "E-VERIFY-LENGTH", "detail": f"{len(body)}자 > {sk.MAX_CUSTOMER_TEXT}"})

    # 5. 상태 문장 존재 (조립기 불변식)
    for s in status_sentences or []:
        if s not in body:
            fails.append({"code": "E-VERIFY-STATUS", "detail": f"상태 문장 누락: {s[:30]}…"})

    # 중복 제거
    seen, uniq = set(), []
    for f in fails:
        k = (f["code"], f["detail"])
        if k not in seen:
            seen.add(k); uniq.append(f)
    return VerifyResult(ok=not uniq, failures=uniq)


def _cli(paths: list[str]) -> int:
    """저장된 평가 결과(.jsonl)에 검증기를 사후 적용한다. 코드만 돌리므로 비용 0.
      python -m app.answer.verify results/A_1025_asm_opus-5-5.jsonl [...]"""
    import json
    from collections import Counter
    for p in paths:
        rows = [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()]
        drafted = [r for r in rows if r.get("draft") and r.get("status") not in ("REJECTED", "ERROR")]
        codes: Counter = Counter()
        bad = []
        for r in drafted:
            v = verify(r["draft"], r.get("figures"), r.get("sources") or [])
            if not v.ok:
                codes.update(v.codes); bad.append((r["case_id"], v.failures))
        print(f"\n## {p}: draft {len(drafted)}건 중 검증 실패 {len(bad)}건  코드별 {dict(codes)}")
        for cid, fs in bad[:20]:
            print(f"  {cid}: " + "; ".join(f"{f['code']} {f['detail']}" for f in fs[:3]))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_cli(sys.argv[1:]))
