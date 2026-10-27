"""평가 trace DB 에서 sql_query 툴이 실제로 만든·실행한 SQL 을 뽑아 본다 (SCRUM-26 품질 확인).

    python scripts/show_sql.py results/trace_A_t2sql.db
    python scripts/show_sql.py results/trace_A_t2sql.db --rejected   # 거부·재생성이 있었던 호출만
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("--rejected", action="store_true")
    a = ap.parse_args()
    conn = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    rows = conn.execute("SELECT trace_id, step_no, args_json, observation FROM agent_step WHERE tool = 'sql_query' ORDER BY rowid").fetchall()
    how, rej, errs = Counter(), Counter(), Counter()
    for tid, no, args, obs in rows:
        try:
            o = json.loads(obs)
        except Exception:
            errs["(파싱 불가 observation)"] += 1
            continue
        how[o.get("generated_by", o.get("error", "?"))] += 1
        attempts = o.get("attempts") or []
        for at in attempts:
            if at.get("rejected"):
                rej[at["rejected"]] += 1
        if o.get("error"):
            errs[o["error"]] += 1
        if a.rejected and not attempts:
            continue
        intent = (json.loads(args) if args else {}).get("intent", "")
        print(f"\n[{tid[:8]} #{no}] 의도: {intent}")
        for at in attempts:
            print(f"   거부 {at.get('rejected')} {at.get('detail', '')}: {at.get('sql', at.get('error', ''))[:160]}")
        print(f"   {o.get('generated_by', o.get('error'))} rows={o.get('row_count')} {o.get('elapsed_ms', '')}ms")
        print(f"   {o.get('sql', '')[:300]}")
    print(f"\n호출 {len(rows)}건 | 생성 경로 {dict(how)} | 거부 사유 {dict(rej)} | 오류 {dict(errs)}")


if __name__ == "__main__":
    main()
