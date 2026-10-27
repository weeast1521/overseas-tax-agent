"""sql_query 툴이 실제로 만든·실행한 SQL 을 본다 (SCRUM-26 품질 확인).

    python scripts/show_sql.py results/sql_A_t2sql3.jsonl             # SQL_LOG (전문, 권장)
    python scripts/show_sql.py results/trace_A_t2sql3.db              # trace DB (observation 2,000자 잘림 → 긴 SQL 은 못 봄)
    python scripts/show_sql.py <파일> --rejected                        # 거부·재생성·오류가 있었던 호출만
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path


def _from_log(p: Path) -> list[dict]:
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def _from_trace(p: Path) -> list[dict]:
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    out = []
    for tid, no, args, obs in conn.execute("SELECT trace_id, step_no, args_json, observation FROM agent_step WHERE tool = 'sql_query' ORDER BY rowid"):
        intent = (json.loads(args) if args else {}).get("intent", "")
        try:
            o = json.loads(obs)
        except Exception:
            out.append({"intent": intent, "result": "(observation 잘림 — SQL_LOG 를 쓰세요)", "attempts": [], "sql": (obs or "")[:300]})
            continue
        out.append({"intent": intent, "result": o.get("generated_by") or o.get("error"), "message": o.get("message"), "attempts": o.get("attempts") or [],
                    "sql": o.get("sql", ""), "row_count": o.get("row_count"), "elapsed_ms": o.get("elapsed_ms")})
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("--rejected", action="store_true")
    a = ap.parse_args()
    p = Path(a.src)
    recs = _from_trace(p) if p.suffix == ".db" else _from_log(p)
    how, rej, gen_ms, lens = Counter(), Counter(), [], []
    for r in recs:
        how[r.get("result")] += 1
        for at in r.get("attempts", []):
            if at.get("rejected"):
                rej[at["rejected"]] += 1
        if r.get("gen_ms") is not None:
            gen_ms.append(r["gen_ms"])
        if r.get("sql"):
            lens.append(len(r["sql"]))
        if a.rejected and not r.get("attempts") and not str(r.get("result", "")).startswith("E-"):
            continue
        print(f"\n의도: {r.get('intent', '')}")
        for at in r.get("attempts", []):
            print(f"   거부 {at.get('rejected') or at.get('error')} {at.get('detail', '')}\n      {at.get('sql', '')[:400]}")
        print(f"   → {r.get('result')} rows={r.get('row_count')} gen={r.get('gen_ms', '?')}ms {r.get('message') or ''}")
        print(f"   {r.get('sql', '')[:500]}")
    avg = lambda xs: int(sum(xs) / len(xs)) if xs else 0
    print(f"\n호출 {len(recs)}건 | 결과 {dict(how)} | 거부 사유 {dict(rej)} | 생성 평균 {avg(gen_ms)}ms | SQL 평균 길이 {avg(lens)}자")


if __name__ == "__main__":
    main()
