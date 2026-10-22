"""SCRUM-21: ClaudeJudge를 합성 민원 20건에 돌려 분류 정확도·토큰 사용량을 본다.

  export ANTHROPIC_API_KEY=sk-ant-...
  python scripts/judge_eval.py [--model claude-haiku-5-5] [--cases data/eval/judge_cases.json] [--limit 5]

합성 문장만 보낸다. 실데이터(data/mine)는 읽지 않는다.
결과 JSON은 results/ (gitignore)에 저장된다. 수용 조건: 20건 중 18건 이상.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.judge.claude_judge import ClaudeJudge, JudgeError  # noqa: E402


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=None)
    ap.add_argument("--cases", default=str(ROOT / "data" / "eval" / "judge_cases.json"))
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    if not os.getenv("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY가 없습니다. export ANTHROPIC_API_KEY=sk-ant-... 후 다시 실행하세요.")
        return 2

    cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))[: args.limit]
    judge = ClaudeJudge(model=args.model)
    print(f"model={judge.model}  cases={len(cases)}\n")

    rows, ok, tin, tout = [], 0, 0, 0
    for c in cases:
        t0 = time.perf_counter()
        try:
            j = await judge.classify(c["text"])
            got, conf, flags, err = j.category, j.confidence, j.flags, None
        except JudgeError as e:
            got, conf, flags, err = None, None, [], f"{e.code} {e}"
        ms = int((time.perf_counter() - t0) * 1000)
        u = judge.last_usage
        tin += u["in_tokens"]; tout += u["out_tokens"]
        hit = got == c["expected"]
        ok += hit
        rows.append({**c, "got": got, "confidence": conf, "flags": flags, "error": err, "ok": hit, "latency_ms": ms, **u})
        mark = "O" if hit else "X"
        shown = got or err
        print(f"{mark} {c['id']} 기대 {c['expected']:<15} 결과 {shown:<15} conf {conf if conf is not None else '-':<5} {ms:>5}ms  {c['text'][:38]}")

    n = len(cases)
    print(f"\n정확도 {ok}/{n} ({ok / n:.0%})   수용 조건 18/20 이상: {'통과' if ok >= 18 * n / 20 else '미달'}")
    print(f"토큰 평균 입력 {tin / n:.0f}  출력 {tout / n:.0f}  (총 입력 {tin}, 출력 {tout})")
    wrong = [r for r in rows if not r["ok"]]
    if wrong:
        print("\n틀린 건:")
        for r in wrong:
            print(f"  {r['id']} 기대 {r['expected']} → {r['got'] or r['error']}  \"{r['text']}\"")

    out = ROOT / "results" / f"judge_eval_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"model": judge.model, "accuracy": ok / n, "avg_in_tokens": tin / n,
                               "avg_out_tokens": tout / n, "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n저장: {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
