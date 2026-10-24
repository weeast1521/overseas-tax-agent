"""SCRUM-22 입력: Judge만 평가셋 200문장에 n회 돌려 confidence의 흔들림과 모델별 분포를 잰다. 에이전트는 돌리지 않는다 (싸다).

  python -m eval.judge_sweep --model claude-haiku-5-5 --repeat 5      # Haiku: 1,000 호출 ≈ $0.3
  python -m eval.judge_sweep --model claude-sonnet-5-5 --repeat 1     # Sonnet Judge 비교 ≈ $1
  python -m eval.judge_sweep --report results/judge_sweep_haiku-5-5.jsonl [results/judge_sweep_sonnet-5-5.jsonl ...]

무엇을 보나:
  - 같은 문장의 confidence 표준편차·범위 (실행마다 얼마나 흔들리나) → 임계값 안전 여유폭
  - 임계값 후보(0.70/0.75/0.80/0.85/0.90/0.95)별: AUTO로 나가는 비율, 그중 분류 오답(=AUTO 오답률), REVIEW로 빠지는 비율
  - 분류가 실행마다 바뀌는 문장 (경계 사례 목록)
  - REG 질문 중 OUT_OF_SCOPE로 분류된 것 (범위 문제)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

THRESHOLDS = [0.70, 0.75, 0.80, 0.85, 0.90, 0.95]


def load(p: Path) -> list[dict]:
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


async def sweep(model: str, cases: list[dict], repeat: int, out: Path) -> None:
    from app.judge.claude_judge import ClaudeJudge, JudgeError
    judge = ClaudeJudge(model=model)
    tin = tout = 0
    with out.open("w", encoding="utf-8") as f:
        for r in range(1, repeat + 1):
            t0 = time.perf_counter()
            for i, c in enumerate(cases, 1):
                try:
                    j = await judge.classify(c["text"])
                    row = {"run": r, "case_id": c["case_id"], "kind": c["kind"], "expected": c["category"],
                           "category": j.category, "confidence": j.confidence, "flags": j.flags, "error": None}
                except JudgeError as e:
                    row = {"run": r, "case_id": c["case_id"], "kind": c["kind"], "expected": c["category"],
                           "category": None, "confidence": None, "flags": [], "error": f"{e.code}: {str(e)[:160]}"}
                    if i == 1:
                        print(f"  첫 호출 실패: {row['error']}", flush=True)
                tin += judge.last_usage["in_tokens"]; tout += judge.last_usage["out_tokens"]
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
                if i % 50 == 0:
                    print(f"  run {r}/{repeat}  {i}/{len(cases)}", flush=True)
            print(f"run {r} 완료 {(time.perf_counter() - t0) / 60:.1f}분", flush=True)
    price = {"claude-haiku-5-5": (0.2, 1.0), "claude-sonnet-5-5": (2.0, 10.0), "claude-opus-5-5": (4.0, 20.0)}.get(model, (0, 0))
    print(f"토큰 {tin:,}/{tout:,} → ≈ ${tin / 1e6 * price[0] + tout / 1e6 * price[1]:.2f}  → {out}")


def report(paths: list[Path]) -> None:
    for p in paths:
        rows = load(p)
        model = p.stem.replace("judge_sweep_", "")
        runs = sorted({r["run"] for r in rows})
        by_case: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            by_case[r["case_id"]].append(r)
        print(f"\n## {model} — {len(runs)}회 × {len(by_case)}문장")

        errs = Counter(r["error"] for r in rows if r["error"])
        if errs:
            print("오류:", dict(errs.most_common(3)))
        if not any(r["confidence"] is not None for r in rows):
            print("유효한 결과 없음 — 전부 실패"); continue
        # 1. confidence 흔들림
        sds = [statistics.pstdev([x["confidence"] for x in v if x["confidence"] is not None]) for v in by_case.values() if len(v) > 1]
        rngs = [max(x["confidence"] for x in v) - min(x["confidence"] for x in v) for v in by_case.values() if len(v) > 1 and all(x["confidence"] is not None for x in v)]
        if sds:
            print(f"confidence 흔들림: 표준편차 평균 {statistics.mean(sds):.3f}, 최대 범위(max-min) 평균 {statistics.mean(rngs):.3f} / 최대 {max(rngs):.2f}")
        flip = [cid for cid, v in by_case.items() if len({x['category'] for x in v}) > 1]
        print(f"실행마다 분류가 바뀐 문장: {len(flip)}건 {flip[:10]}")

        # 2. 임계값 표 (분류 대상: TAX+OOS, 정답 있는 것)
        graded = [r for r in rows if r["kind"] in ("TAX", "OOS") and r["confidence"] is not None]
        print("\n| AUTO 임계값 | AUTO 비율 | AUTO 중 분류 오답 | REVIEW(<0.70) 비율 |")
        print("| --- | --- | --- | --- |")
        for t in THRESHOLDS:
            auto = [r for r in graded if r["confidence"] >= t]
            wrong = sum(r["category"] != r["expected"] for r in auto)
            low = sum(r["confidence"] < 0.70 for r in graded)
            print(f"| {t:.2f} | {len(auto) / len(graded):.0%} | {wrong}/{len(auto)} ({(wrong / len(auto)) if auto else 0:.1%}) | {low / len(graded):.0%} |")

        # 3. 분류 정확도·분포
        acc = sum(r["category"] == r["expected"] for r in graded) / len(graded)
        dist = Counter(round(r["confidence"], 1) for r in graded)
        print(f"\n분류 정확도(전 실행 평균) {acc:.1%}   confidence 분포: {dict(sorted(dist.items()))}")

        # 4. REG → OUT_OF_SCOPE
        reg = [r for r in rows if r["kind"] == "REG"]
        oos = Counter(r["case_id"] for r in reg if r["category"] == "OUT_OF_SCOPE")
        print(f"REG 60문장 중 OUT_OF_SCOPE 판정: {len(oos)}문장 (실행 {len(runs)}회 중 판정 횟수: {dict(oos.most_common(20))})")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="claude-haiku-5-5")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--cases", default=str(ROOT / "eval" / "cases.jsonl"))
    ap.add_argument("--report", nargs="*")
    args = ap.parse_args()
    if args.report:
        report([Path(p) for p in args.report]); return 0
    import os
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY 없음"); return 2
    out = ROOT / "results" / f"judge_sweep_{args.model.replace('claude-', '')}.jsonl"
    out.parent.mkdir(exist_ok=True)
    asyncio.run(sweep(args.model, load(Path(args.cases)), args.repeat, out))
    print(f"다음: python -m eval.judge_sweep --report {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
