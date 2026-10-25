"""SCRUM-43 (F10-6, F10-9): 평가 러너 — cases.jsonl을 구성 A/B/stub으로 돌려 결과를 남긴다.

  python -m eval.run --config A [--cases eval/cases.jsonl] [--tag 20261024] [--limit 10] [--only C001,C002] [--resume]
  python -m eval.run --config A --estimate        # 10건만 돌려 200건 예상 비용 출력
  python -m eval.run --config stub                 # API 없이 규칙 Judge + 대본 LLM (재현성 테스트·러너 점검용)

구성:
  A    = ClaudeJudge(haiku) + ClaudeClient(opus)   ← 크레딧 사용. TRADES_FILE은 data/synthetic 강제
  B    = RuleJudge + OllamaClient                  ← 폐쇄망 (S2, LocalJudge 전까지 규칙 Judge)
  stub = RuleJudge + 대본 LLM                      ← 비용 0, 결정론

출력: results/<config>_<tag>.jsonl (케이스 1줄: 응답 전체 + trace_id + 사용 툴), results/<config>_<tag>.meta.json (설정·모델·코퍼스 버전·git·날짜).
--resume: 같은 파일에 이미 있는 case_id는 건너뛴다 (중간 실패 후 이어서).
재현성: temperature는 SDK 2.x에서 제거됨 → "같은 설정으로 2회 실행 후 report --diff" 로 측정한다.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PRICE_PER_M = {"claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-5-5": (0.2, 1.0), "claude-sonnet-4-5": (3.0, 15.0)}

CONFIGS = {
    "A": {"JUDGE_IMPL": "claude", "LLM_PROVIDER": "claude"},
    "B": {"JUDGE_IMPL": "rule", "LLM_PROVIDER": "ollama"},
    "stub": {"JUDGE_IMPL": "rule", "LLM_PROVIDER": "stub"},
}


def file_hash(paths: list[Path]) -> str:
    h = hashlib.sha256()
    for p in sorted(paths):
        h.update(p.name.encode()); h.update(p.read_bytes())
    return h.hexdigest()[:12]


def git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True, timeout=5).stdout.strip() or "?"
    except Exception:
        return "?"


def load_cases(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


class StubLLM:
    """대본 LLM: sql_query → tax_calc → (reg_search) → 최종 JSON. 결정론. 코퍼스 키워드로 sources를 채운다."""
    name = "stub"
    model = "stub-v1"

    def __init__(self, category: str):
        self.category = category
        self.n = 0

    async def complete(self, messages, tools):
        from app.llm.base import Completion, ToolCall
        self.n += 1
        if self.n == 1:
            return Completion(tool_call=ToolCall(id="s1", name="sql_query", args={"intent": "전체 거래"}), in_tokens=1000, out_tokens=30)
        if self.n == 2:
            return Completion(tool_call=ToolCall(id="s2", name="tax_calc", args={}), in_tokens=1500, out_tokens=30)
        if self.n == 3:
            q = {"TRANSFER_BASIS": "취득가액 이체", "MULTI_BROKER": "합산 여러 증권사", "FX_BASIS": "환율 결제일", "OFFSET": "통산 손실"}.get(self.category, "기본공제 세율")
            return Completion(tool_call=ToolCall(id="s3", name="reg_search", args={"query": q}), in_tokens=1800, out_tokens=30)
        obs = next((m.content for m in reversed(messages) if m.role == "tool" and m.name == "tax_calc"), "{}")
        reg = next((m.content for m in reversed(messages) if m.role == "tool" and m.name == "reg_search"), "{}")
        import re
        m_ = re.search(r'"tax_krw":\s*(-?\d+)', obs)  # Observation은 2,000자에서 잘리므로 JSON 전체 파싱 대신 앞쪽 summary에서 읽는다 (실제 LLM도 그렇게 본다)
        tax = int(m_.group(1)) if m_ else None
        try:
            chunks = json.loads(reg).get("chunks", [])[:2]
        except Exception:
            chunks = []
        final = {"summary": f"올해 세액은 {tax}원입니다." if tax is not None else "계산 불가",
                 "figures": {"tax_krw": tax}, "sources": [{"law": c["law"], "article": c["article"]} for c in chunks]}
        return Completion(text=json.dumps(final, ensure_ascii=False), in_tokens=2200, out_tokens=120)


def parse_sources(draft: str | None) -> list[dict]:
    if not draft:
        return []
    s = draft.strip()
    if s.startswith("```"):
        s = s.strip("`")
        s = s[s.find("{"):] if "{" in s else s
    try:
        j = json.loads(s[s.find("{"): s.rfind("}") + 1])
        return [x for x in j.get("sources", []) if isinstance(x, dict) and "law" in x]
    except Exception:
        return []


async def run_cases(cases: list[dict], config: str, out: Path, resume: bool) -> list[dict]:
    from app import main, trace
    from app.schemas import InquiryRequest

    done: set[str] = set()
    if resume and out.exists():
        done = {json.loads(l)["case_id"] for l in out.read_text(encoding="utf-8").splitlines() if l.strip()}
        print(f"이어서: 이미 {len(done)}건 있음")
    elif out.exists():
        out.unlink()

    if config == "stub":
        main._llm = lambda: StubLLM(_current["category"])  # 케이스별 대본
    # REG/OOS는 고객 무관 — 거래가 있는 합성 고객 하나로 고정 (figures가 비지 않게)
    default_cust = next((c["customer_id"] for c in cases if c["customer_id"] is not None), None) or int(os.getenv("EVAL_DEFAULT_CUSTOMER", "2001"))
    rows = []
    t_all = time.perf_counter()
    with out.open("a", encoding="utf-8") as f:
        for i, c in enumerate(cases, 1):
            if c["case_id"] in done:
                continue
            _current["category"] = c["category"]
            cust = c["customer_id"] if c["customer_id"] is not None else default_cust
            t0 = time.perf_counter()
            try:
                resp, agent = await main.handle_inquiry(InquiryRequest(customer_id=cust, tax_year=main.TAX_YEAR, text=c["text"]))
                r = resp.model_dump(mode="json")
                r["sources"] = r["sources"] or parse_sources(resp.draft)
                r["tools"] = [s.tool for s in agent.steps if s.tool] if agent else []
                r["off_plan"] = [s.tool for s in agent.steps if s.off_plan] if agent else []
                r["aborted"] = agent.aborted if agent else None
                r["error"] = None
            except Exception as e:  # 러너는 멈추지 않는다
                r = {"status": "ERROR", "error": f"{type(e).__name__}: {str(e)[:200]}", "trace_id": None, "tools": [], "tokens_in": 0, "tokens_out": 0,
                     "category": None, "confidence": None, "figures": None, "draft": None, "sources": [], "steps": 0, "latency_ms": 0, "review_reason": None, "error_code": None}
            r["wall_ms"] = int((time.perf_counter() - t0) * 1000)
            row = {"case_id": c["case_id"], "kind": c["kind"], "category_expected": c["category"], "customer_id": cust, **r}
            rows.append(row)
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n"); f.flush()
            mark = "E" if r["status"] == "ERROR" else "."
            print(f"{mark} {i:3}/{len(cases)} {c['case_id']} {str(r.get('category')):<15} {r['status']:<8} {str(r.get('review_reason') or r.get('error_code') or ''):<18} "
                  f"steps {r.get('steps', 0)} tok {r.get('tokens_in', 0)}/{r.get('tokens_out', 0)} {row['wall_ms'] / 1000:.1f}s", flush=True)
    print(f"\n총 {len(rows)}건 {(time.perf_counter() - t_all) / 60:.1f}분")
    return rows


_current: dict = {"category": None}


def main_cli() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", choices=list(CONFIGS), required=True)
    ap.add_argument("--cases", default=str(ROOT / "eval" / "cases.jsonl"))
    ap.add_argument("--tag", default=datetime.now().strftime("%Y%m%d_%H%M"))
    ap.add_argument("--limit", type=int)
    ap.add_argument("--only")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--estimate", action="store_true", help="10건만 돌려 200건 예상 비용")
    ap.add_argument("--out-dir", default=str(ROOT / "results"))
    ap.add_argument("--trades", default=str(ROOT / "data" / "synthetic" / "trades.json"), help="평가용 거래 파일. .env의 TRADES_FILE보다 우선")
    ap.add_argument("--model", help="구성 A의 에이전트 모델 덮어쓰기 (예: claude-sonnet-5-5). 결과 파일명에 붙는다")
    ap.add_argument("--repeat", type=int, default=1, help="같은 설정으로 n회 실행 (재현성). tag 뒤에 _r1, _r2 …")
    ap.add_argument("--final-format", choices=["json", "prose"], default="json", help="에이전트 최종 답변 형식. prose = 프롬프트만으로 고객용 문장 (대조 실험)")
    args = ap.parse_args()
    os.environ["AGENT_FINAL_FORMAT"] = args.final_format
    if args.final_format != "json":
        args.tag = f"{args.tag}_{args.final_format}"
    if args.model:
        os.environ["CLAUDE_MODEL"] = args.model
        args.tag = f"{args.tag}_{args.model.replace('claude-', '')}"

    for k, v in CONFIGS[args.config].items():
        os.environ[k] = v
    os.environ["TRADES_FILE"] = args.trades  # .env(TRADES_FILE=data/samples…)가 평가를 덮어쓰지 않도록 강제. 평가셋 고객은 synthetic에만 있다
    if args.config == "A":
        if not os.getenv("ANTHROPIC_API_KEY"):
            print("ANTHROPIC_API_KEY 없음 (.env)"); return 2
        from app.llm.claude_client import assert_synthetic_data
        assert_synthetic_data()
    os.environ.setdefault("TRACE_DB", str(Path(args.out_dir) / f"trace_{args.config}_{args.tag}.db"))

    cases = load_cases(Path(args.cases))
    known = {int(r["customer_id"]) for r in json.loads(Path(args.trades).read_text(encoding="utf-8"))}
    missing = sorted({c["customer_id"] for c in cases if c["customer_id"] is not None and c["customer_id"] not in known})
    if missing:  # 돈 쓰기 전에 멈춘다
        print(f"평가셋 고객 {len(missing)}명이 {args.trades}에 없음 (예: {missing[:5]}). --trades 확인"); return 2
    if args.only:
        want = set(args.only.split(",")); cases = [c for c in cases if c["case_id"] in want]
    if args.estimate:
        cases = cases[:10]; args.tag = args.tag + "_estimate"
    if args.limit:
        cases = cases[: args.limit]

    out_dir = Path(args.out_dir); out_dir.mkdir(exist_ok=True)
    out = out_dir / f"{args.config}_{args.tag}.jsonl"
    meta = {
        "config": args.config, "tag": args.tag, "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git_commit": git_commit(), "cases_file": str(Path(args.cases).relative_to(ROOT)) if Path(args.cases).is_relative_to(ROOT) else args.cases,
        "cases_hash": file_hash([Path(args.cases)]), "n_cases": len(cases),
        "corpus_version": file_hash(list((ROOT / "data" / "corpus").glob("*.json"))),
        "trades_file": os.environ["TRADES_FILE"], "trace_db": os.environ["TRACE_DB"],
        "judge_impl": os.environ["JUDGE_IMPL"], "judge_model": os.getenv("JUDGE_MODEL"),
        "llm_provider": os.environ["LLM_PROVIDER"], "llm_model": os.getenv("CLAUDE_MODEL") if os.environ["LLM_PROVIDER"] == "claude" else os.getenv("OLLAMA_MODEL"),
        "temperature": "n/a (SDK 2.x — 재현성은 2회 실행 diff로 측정)",
        "final_format": args.final_format, "review_threshold": os.getenv("JUDGE_REVIEW_THRESHOLD", "0.70"), "auto_threshold": os.getenv("JUDGE_AUTO_THRESHOLD", "0.95"),
    }
    if args.config == "stub":
        meta.update(judge_model="rule", llm_model="stub-v1")
    print(f"config={args.config} judge={meta['judge_impl']}/{meta['judge_model']} llm={meta['llm_provider']}/{meta['llm_model']} "
          f"cases={len(cases)} corpus={meta['corpus_version']} git={meta['git_commit']}\n→ {out}\n")

    from app import main  # noqa: F401  (환경변수 설정 후 import)
    if args.repeat > 1:
        base_tag = args.tag
        for i in range(1, args.repeat + 1):
            sub = [sys.executable, "-m", "eval.run", "--config", args.config, "--cases", args.cases, "--tag", f"{base_tag}_r{i}", "--out-dir", args.out_dir]
            if args.limit: sub += ["--limit", str(args.limit)]
            if args.only: sub += ["--only", args.only]
            print(f"\n===== 실행 {i}/{args.repeat} ({base_tag}_r{i}) =====")
            subprocess.run(sub, cwd=ROOT, check=False)
        print(f"\n재현성 확인: python -m eval.report {out_dir}/{args.config}_{base_tag}_r1.jsonl --diff {out_dir}/{args.config}_{base_tag}_r2.jsonl")
        return 0
    rows = asyncio.run(run_cases(cases, args.config, out, args.resume))
    meta["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    tin, tout = sum(r.get("tokens_in", 0) for r in rows), sum(r.get("tokens_out", 0) for r in rows)
    pin, pout = PRICE_PER_M.get(meta.get("llm_model") or "", (0, 0))
    meta["tokens_in"], meta["tokens_out"] = tin, tout
    meta["est_cost_usd"] = round(tin / 1e6 * pin + tout / 1e6 * pout, 3)
    (out_dir / f"{args.config}_{args.tag}.meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"토큰 입력 {tin:,} / 출력 {tout:,} → 추정 ${meta['est_cost_usd']:.2f} (에이전트, Judge 제외)")
    if args.estimate and rows:
        per = meta["est_cost_usd"] / len(rows)
        print(f"200건 예상: 약 ${per * 200:.1f}, {sum(r['wall_ms'] for r in rows) / len(rows) / 1000 * 200 / 60:.0f}분. 잔액 확인 후 --estimate 없이 실행")
    print(f"다음: python -m eval.report {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main_cli())
