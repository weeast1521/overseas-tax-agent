"""SCRUM-25: 합성 민원 10건을 실제 Claude(Judge + 에이전트)로 끝까지 돌린다.

  python scripts/run_e2e.py [--cases data/eval/e2e_cases.json] [--limit 3] [--only E01,E04]

전제: .env에 ANTHROPIC_API_KEY, CLAUDE_MODEL(에이전트), JUDGE_MODEL(Judge). TRADES_FILE은 data/samples(합성)만.
출력: 건별 status·steps·토큰·지연 표, 수용 조건 판정. 스텝별 trace와 외부로 나간 payload 요약은 results/e2e_*.json에.

수용 조건 (Jira SCRUM-25):
  - 10건 중 8건 이상 final 도달(aborted 아님), 평균 스텝 ≤ 6
  - 각 건 스텝이 남고 tokens_in/out > 0
  - tax_calc가 호출된 건은 final에 tax_krw 숫자가 그대로 들어 있다
  - 외부 호출 payload에 data/mine 내용이 없다
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("JUDGE_IMPL", "claude")
os.environ.setdefault("LLM_PROVIDER", "claude")

from app import main  # noqa: E402
from app.llm.claude_client import ClaudeClient, assert_synthetic_data  # noqa: E402
from app.schemas import InquiryRequest  # noqa: E402

PRICE_PER_M = {  # USD / 1M tokens. 공식 단가는 콘솔에서 확인. 여기 값은 추정용
    "claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-5-5": (0.2, 1.0),
    "claude-sonnet-4-5": (3.0, 15.0),
}


class PayloadRecorder:
    """외부로 나가는 요청을 가로채 기록한다 (데이터 경계 확인용). 호출은 그대로 통과."""

    def __init__(self):
        self.payloads: list[dict] = []

    def wrap(self, client: ClaudeClient):
        orig = client.client.messages.create

        async def create(**kw):
            self.payloads.append({"model": kw.get("model"), "messages": kw.get("messages"), "system": kw.get("system")})
            return await orig(**kw)

        client.client.messages.create = create
        return client


def extract_numbers(text: str) -> set[str]:
    return set(re.findall(r"\d[\d,]*", text or ""))


async def main_async() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default=str(ROOT / "data" / "eval" / "e2e_cases.json"))
    ap.add_argument("--limit", type=int)
    ap.add_argument("--only", help="쉼표로 구분한 케이스 id")
    args = ap.parse_args()

    if not os.getenv("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY가 없습니다 (.env 확인)")
        return 2
    assert_synthetic_data()  # data/mine이면 여기서 멈춘다
    mine_markers = _mine_markers()

    cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    if args.only:
        want = set(args.only.split(","))
        cases = [c for c in cases if c["id"] in want]
    cases = cases[: args.limit]

    rec = PayloadRecorder()
    llm = rec.wrap(ClaudeClient())
    main._llm = lambda: llm  # 같은 클라이언트 재사용 (payload 기록)
    judge = main._judge()
    main._judge = lambda: judge
    print(f"agent={llm.model}  judge={getattr(judge, 'model', judge.name)}  cases={len(cases)}  TRADES_FILE={os.getenv('TRADES_FILE', 'data/samples/trades.json')}\n")

    rows = []
    for c in cases:
        t0 = time.perf_counter()
        resp, agent = await main.handle_inquiry(InquiryRequest(customer_id=c["customer_id"], tax_year=main.TAX_YEAR, text=c["text"]))
        wall = int((time.perf_counter() - t0) * 1000)
        steps = agent.steps if agent else []
        tools = [s.tool for s in steps if s.tool]
        final = resp.draft or ""
        tax_krw = (resp.figures or {}).get("tax_krw")
        tax_in_final = (str(tax_krw) in final.replace(",", "")) if ("tax_calc" in tools and tax_krw is not None) else None
        reached = agent is not None and agent.final is not None
        row = {
            "id": c["id"], "customer_id": c["customer_id"], "expected_category": c["expected_category"],
            "category": resp.category, "confidence": resp.confidence, "status": resp.status, "review_reason": resp.review_reason,
            "error_code": resp.error_code, "reached_final": reached, "aborted": agent.aborted if agent else None,
            "steps": len(steps), "tools": tools, "off_plan": [s.tool for s in steps if s.off_plan],
            "tokens_in": resp.tokens_in, "tokens_out": resp.tokens_out, "latency_ms": resp.latency_ms, "wall_ms": wall,
            "tax_krw": tax_krw, "tax_in_final": tax_in_final,
            "trace": [{"step": s.step_no, "tool": s.tool, "args": s.args, "obs": (s.observation or "")[:300],
                       "error": s.error_code, "latency_ms": s.latency_ms, "in": s.in_tokens, "out": s.out_tokens} for s in steps],
            "final": final,
        }
        rows.append(row)
        mark = "O" if reached else ("-" if agent is None else "X")
        cat_ok = "" if resp.category == c["expected_category"] else f" (기대 {c['expected_category']})"
        print(f"{mark} {c['id']} {resp.category:<14}{cat_ok} {resp.status:<8} {str(resp.review_reason or resp.error_code or ''):<18} "
              f"steps {len(steps)} tools {'>'.join(tools) or '-'}  tok {resp.tokens_in}/{resp.tokens_out}  {wall/1000:.1f}s"
              f"{'  tax_krw 불일치' if tax_in_final is False else ''}")

    # 데이터 경계: 외부로 나간 payload에 실데이터 흔적이 없는지
    blob = json.dumps(rec.payloads, ensure_ascii=False)
    leaks = [m for m in mine_markers if m in blob]

    ran = [r for r in rows if r["aborted"] is not None or r["reached_final"]]
    n_final = sum(r["reached_final"] for r in rows)
    avg_steps = (sum(r["steps"] for r in ran) / len(ran)) if ran else 0
    tin, tout = sum(r["tokens_in"] for r in rows), sum(r["tokens_out"] for r in rows)
    pin, pout = PRICE_PER_M.get(llm.model, (0, 0))
    cost = tin / 1e6 * pin + tout / 1e6 * pout
    over30 = [r["id"] for r in rows if r["wall_ms"] > 30_000]
    tax_bad = [r["id"] for r in rows if r["tax_in_final"] is False]
    cat_bad = [r["id"] for r in rows if r["category"] != r["expected_category"]]

    print(f"\nfinal 도달 {n_final}/{len(rows)} (기준 8/10)   평균 스텝 {avg_steps:.1f} (기준 ≤ 6)")
    print(f"에이전트 토큰 합계 입력 {tin:,} / 출력 {tout:,}  → 추정 비용 ${cost:.2f} ({llm.model}, Judge 제외)")
    print(f"30초 초과: {over30 or '없음'}   tax_krw 불일치: {tax_bad or '없음'}   분류 불일치: {cat_bad or '없음'}")
    print(f"데이터 경계: 외부 payload {len(rec.payloads)}건에 실데이터 흔적 {'없음 ✓' if not leaks else '있음 ✗ ' + str(leaks)}")

    out = ROOT / "results" / f"e2e_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"agent_model": llm.model, "judge_model": getattr(judge, "model", None),
                               "n_final": n_final, "avg_steps": avg_steps, "tokens_in": tin, "tokens_out": tout,
                               "est_cost_usd": round(cost, 3), "leaks": leaks, "rows": rows}, ensure_ascii=False, indent=2, default=str),
                   encoding="utf-8")
    print(f"저장: {out.relative_to(ROOT)}  (trace·final 전문 포함)")
    print(f"agent_trace DB: {os.getenv('TRACE_DB', 'data/trace.db')}  → GET /v1/traces/<trace_id> 로 스텝 조회 가능")
    return 0 if (n_final >= 8 * len(rows) / 10 and not leaks) else 1


def _mine_markers() -> list[str]:
    """data/mine/trades.json이 있으면 그 안의 trade_id·계좌 id를 마커로 쓴다. 없으면 빈 목록."""
    p = ROOT / "data" / "mine" / "trades.json"
    if not p.exists():
        return []
    try:
        rows = json.loads(p.read_text(encoding="utf-8"))
        return sorted({str(r.get(k)) for r in rows for k in ("trade_id", "account_id") if r.get(k)})
    except Exception:
        return []


if __name__ == "__main__":
    sys.exit(asyncio.run(main_async()))
