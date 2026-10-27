"""30초 예산이 어디로 가는지 — trace DB 의 구간별 ms (judge / 에이전트 LLM / 툴별 / 조립기).

    python scripts/show_latency.py results/trace_A_t2sql5.db
"""
from __future__ import annotations

import json
import sqlite3
import sys
from statistics import mean


def main(db: str) -> None:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = conn.execute("SELECT trace_id, status, error_code, latency_ms, phases_json FROM agent_trace ORDER BY created_at").fetchall()
    steps = {}
    for tid, no, tool, lat, tool_ms in conn.execute("SELECT trace_id, step_no, tool, latency_ms, tool_ms FROM agent_step ORDER BY trace_id, step_no"):
        steps.setdefault(tid, []).append((tool or "final", lat, tool_ms or 0))
    agg = {"judge": [], "agent_llm": [], "assembler": [], "total": []}
    tools: dict[str, list[int]] = {}
    print(f"{'trace':9} {'status':7} {'total':>6} {'judge':>6} {'LLM합':>6} {'툴합':>6} {'조립':>6}  스텝(LLM/툴 ms)")
    for tid, status, err, total, pj in rows:
        ph = json.loads(pj) if pj else {}
        st = steps.get(tid, [])
        tsum = sum(t for _, _, t in st)
        for name, _, t in st:
            if name != "final":
                tools.setdefault(name, []).append(t)
        for k in ("judge", "agent_llm", "assembler"):
            if k in ph:
                agg[k].append(ph[k])
        agg["total"].append(total)
        detail = " ".join(f"{n}:{l}/{t}" for n, l, t in st)
        print(f"{tid[:9]:9} {(err or status):7} {total:>6} {ph.get('judge', ''):>6} {ph.get('agent_llm', ''):>6} {tsum:>6} {ph.get('assembler', ''):>6}  {detail}")
    f = lambda xs: f"{int(mean(xs))}" if xs else "-"
    print(f"\n평균 ms — total {f(agg['total'])} | judge {f(agg['judge'])} | 에이전트 LLM {f(agg['agent_llm'])} | 조립기 {f(agg['assembler'])}")
    print("툴 평균 ms — " + ", ".join(f"{k} {f(v)}" for k, v in tools.items()))


if __name__ == "__main__":
    main(sys.argv[1])
