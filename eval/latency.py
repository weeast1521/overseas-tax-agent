"""지연 분해 (SCRUM-19) — 평가 실행이 남긴 trace DB에서 '30초가 어디에 쓰였나'를 본다. 비용 0.

  python -m eval.latency results/trace_A_1026_t30_opus-5-5.db

agent_step.latency_ms = 그 스텝의 LLM 호출 시간(툴 실행 시간은 빠짐).
나머지 = 전체 - 에이전트 LLM 합계 = Judge + 툴 실행 + 조립 LLM + 기타.
"""
from __future__ import annotations

import sqlite3
import statistics
import sys
from collections import defaultdict


def main(path: str) -> int:
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    traces = db.execute("SELECT trace_id, category, status, error_code, latency_ms, steps FROM agent_trace").fetchall()
    steps = defaultdict(list)
    for s in db.execute("SELECT trace_id, step_no, tool, latency_ms, out_tokens FROM agent_step ORDER BY trace_id, step_no"):
        steps[s["trace_id"]].append(s)

    rows = []
    for t in traces:
        st = steps.get(t["trace_id"], [])
        if not st:
            continue  # Judge 게이트에서 멈춘 건
        agent = sum(s["latency_ms"] for s in st)
        rows.append({"cat": t["category"], "status": t["status"], "err": t["error_code"], "total": t["latency_ms"], "agent": agent,
                     "rest": t["latency_ms"] - agent, "n": len(st), "per": [s["latency_ms"] for s in st],
                     "tools": [s["tool"] or "final" for s in st], "out": sum(s["out_tokens"] for s in st)})
    if not rows:
        print("에이전트를 돈 trace 없음"); return 1

    def avg(xs):
        return statistics.mean(xs) / 1000 if xs else 0

    print(f"\n## {path} — 에이전트를 돈 {len(rows)}건\n")
    print("| 구분 | n | 전체 | 에이전트 LLM 합계 | 나머지(Judge·툴·조립) | 스텝 수 | 스텝당 LLM | 출력 토큰 |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- |")
    groups = {"전체": rows, "E-TIMEOUT": [r for r in rows if r["err"] == "E-TIMEOUT"], "제한 안": [r for r in rows if r["err"] != "E-TIMEOUT"]}
    for cat in sorted({r["cat"] for r in rows}):
        groups[cat] = [r for r in rows if r["cat"] == cat]
    for name, g in groups.items():
        if not g:
            continue
        print(f"| {name} | {len(g)} | {avg([r['total'] for r in g]):.1f}s | {avg([r['agent'] for r in g]):.1f}s | {avg([r['rest'] for r in g]):.1f}s | "
              f"{statistics.mean(r['n'] for r in g):.1f} | {avg([x for r in g for x in r['per']]):.1f}s | {statistics.mean(r['out'] for r in g):.0f} |")

    # 스텝 위치별 (1번째 LLM 호출, 2번째 …) 평균 — 어느 턴이 느린가
    by_pos = defaultdict(list)
    by_tool = defaultdict(list)
    for r in rows:
        for i, (ms, tool) in enumerate(zip(r["per"], r["tools"]), 1):
            by_pos[i].append(ms)
            by_tool[tool].append(ms)
    print("\n스텝 위치별 LLM 시간: " + ", ".join(f"{i}번째 {avg(v):.1f}s" for i, v in sorted(by_pos.items())))
    print("그 스텝에서 고른 행동별: " + ", ".join(f"{k} {avg(v):.1f}s(n={len(v)})" for k, v in sorted(by_tool.items(), key=lambda x: -avg(x[1]))))
    seq = defaultdict(int)
    for r in rows:
        seq[" → ".join(r["tools"])] += 1
    print("툴 순서: " + "; ".join(f"{k} ×{v}" for k, v in sorted(seq.items(), key=lambda x: -x[1])[:5]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
