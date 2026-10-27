"""SCRUM-33·34 검색 평가 — 규정 질문(REG 55)으로 벡터만 / 키워드만 / 하이브리드 / 하이브리드+재작성의 recall@k 비교. LLM 비용 0 (재작성이 dict일 때).

  python scripts/index_corpus.py            # 먼저 bge-m3 색인 (맥, Ollama)
  python -m eval.retrieval [--k 3] [--rewrite dict|llm]

정답 매칭:
  - 법령·시행령 조문: (법, 조) 일치 — 항·part는 따지지 않는다
  - 시험지의 '국세청 해외주식 양도소득세 신고안내 Ⅱ-3' · '증권사 신고대행 FAQ Q7'은 시험지를 만들 때 실제 문서를 보기 전에 붙인 가상 출처라
    문서 종류(국세청안내 / 증권사FAQ) 조각이 상위 k에 있으면 적중으로 본다 (10/27, SCRUM-31 이후)
  - 코퍼스에 없는 정답(국세청 해석 서면-2022-국제세원-0764 등)만 있는 질문은 '답할 수 없음'으로 따로 센다
지표: hit@k(정답 하나라도 상위 k) · recall@k(정답 중 찾은 비율) · MRR · 재작성 횟수 · 약함(weak) 판정 수
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DOC_ALIASES = {"국세청 해외주식 양도소득세 신고안내": "국세청안내", "증권사 신고대행 FAQ": "증권사FAQ"}


def _norm(s: str) -> str:
    return "".join((s or "").split())


def targets(expected: list[list[str]], corpus: list[dict]) -> list[tuple[str, str]]:
    """정답 → 매칭 키. ('ART', '소득세법|제118조의4') 또는 ('DOC', '국세청안내'). 코퍼스에 없는 건 제외."""
    have_art = {(_norm(c["law"]), c["article"]) for c in corpus}
    have_doc = {c.get("doc_type") for c in corpus}
    out = []
    for law, art in expected:
        if law in DOC_ALIASES:
            if DOC_ALIASES[law] in have_doc:
                out.append(("DOC", DOC_ALIASES[law]))
        elif (_norm(law), art) in have_art:
            out.append(("ART", f"{_norm(law)}|{art}"))
    return out


def keys_of(chunk: dict) -> set[tuple[str, str]]:
    return {("ART", f"{_norm(chunk['law'])}|{chunk['article']}"), ("DOC", chunk.get("doc_type") or "")}


def evaluate(searcher, cases: list[dict], k: int, mode: str, rewrite: bool) -> dict:
    rows, unanswerable = [], []
    for c in cases:
        tg = targets(c["expected"]["articles"], searcher.chunks)
        if not tg:
            unanswerable.append(c["case_id"]); continue
        t0 = time.perf_counter()
        r = searcher.search(c["text"], k=k, mode=mode, rewrite=rewrite)
        ms = (time.perf_counter() - t0) * 1000
        got = [keys_of(ch) for ch in r.chunks]
        first = next((i for i, g in enumerate(got, 1) if g & set(tg)), None)
        found = {t for t in tg if any(t in g for g in got)}
        rows.append({"case_id": c["case_id"], "hit": first is not None, "rr": 1 / first if first else 0.0,
                     "recall": len(found) / len(tg), "weak": r.weak, "rewritten": r.rewritten_query, "ms": ms,
                     "top_vec": r.top_vec, "text": c["text"], "want": tg, "got": [f"{ch['law']} {ch['article']}" for ch in r.chunks]})
    n = len(rows)
    return {"mode": mode + ("+재작성" if rewrite else ""), "n": n, "unanswerable": unanswerable,
            "hit": sum(r["hit"] for r in rows) / n, "recall": statistics.mean(r["recall"] for r in rows),
            "mrr": statistics.mean(r["rr"] for r in rows), "weak": sum(r["weak"] for r in rows),
            "rewritten": sum(1 for r in rows if r["rewritten"]), "ms_p50": statistics.median(r["ms"] for r in rows), "rows": rows}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=3)
    ap.add_argument("--rewrite", choices=["dict", "llm"], default="dict")
    ap.add_argument("--cases", default=str(ROOT / "eval" / "cases.jsonl"))
    ap.add_argument("--show-misses", type=int, default=12)
    args = ap.parse_args()
    os.environ["REWRITE"] = args.rewrite
    from app.rag import search
    s = search.get_searcher()
    cases = [json.loads(l) for l in Path(args.cases).read_text(encoding="utf-8").splitlines() if l.strip()]
    cases = [c for c in cases if c["kind"] == "REG"]
    has_vec = s.store is not None
    meta = s.store.active() if has_vec else None
    print(f"\n# 검색 평가 — REG {len(cases)}문항, k={args.k}, 색인 {meta['version'] + ' ' + meta['model'] if meta else '없음(키워드만)'}, "
          f"조각 {len(s.chunks)}, 재작성 {args.rewrite}, WEAK_COS {s.weak_cos}\n")
    configs = [("keyword", False)] + ([("vector", False), ("hybrid", False), ("hybrid", True)] if has_vec else [("keyword", True)])
    results = [evaluate(s, cases, args.k, m, rw) for m, rw in configs]
    print(f"| 방식 | n | hit@{args.k} | recall@{args.k} | MRR | 약함 판정 | 재작성 | 지연 p50 |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for r in results:
        print(f"| {r['mode']} | {r['n']} | {r['hit']:.0%} | {r['recall']:.0%} | {r['mrr']:.2f} | {r['weak']} | {r['rewritten']} | {r['ms_p50']:.0f}ms |")
    print(f"\n답할 수 없음(정답이 코퍼스에 없음): {len(results[0]['unanswerable'])}건 {results[0]['unanswerable']}")
    best = results[-1]
    if has_vec:  # WEAK_COS 보정 입력: 적중/실패별 벡터 1위 코사인
        hv = [r["top_vec"] for r in results[2]["rows"] if r["hit"] and r["top_vec"] is not None]
        mv = [r["top_vec"] for r in results[2]["rows"] if not r["hit"] and r["top_vec"] is not None]
        if hv and mv:
            print(f"벡터 1위 코사인 — 적중 평균 {statistics.mean(hv):.3f} (최소 {min(hv):.3f}) · 실패 평균 {statistics.mean(mv):.3f} (최대 {max(mv):.3f})")
    misses = [r for r in best["rows"] if not r["hit"]]
    print(f"\n## '{best['mode']}' 실패 {len(misses)}건 (상위 {args.show_misses})")
    for r in misses[: args.show_misses]:
        print(f"- {r['case_id']} {r['text'][:50]}\n  정답 {[t[1] for t in r['want']]} · 상위 {r['got']}" + (f"\n  재작성: {r['rewritten']}" if r["rewritten"] else ""))
    out = ROOT / "results" / f"retrieval_k{args.k}_{meta['version'] if meta else 'kw'}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps([{k: v for k, v in r.items()} for r in results], ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"\n저장: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
