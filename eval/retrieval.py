"""SCRUM-33·34 검색 평가 — 규정 질문(REG 55)으로 벡터만 / 키워드만 / 하이브리드 / 하이브리드+재작성의 recall@k 비교. LLM 비용 0 (재작성이 dict일 때).

  python scripts/index_corpus.py            # 먼저 bge-m3 색인 (맥, Ollama)
  python -m eval.retrieval [--k 3] [--rewrite dict|llm]

정답 매칭:
  - 법령·시행령 조문: (법, 조) 일치 — 항·part는 따지지 않는다
  - 시험지의 '국세청 해외주식 양도소득세 신고안내 Ⅱ-3' · '증권사 신고대행 FAQ Q7'은 시험지를 만들 때 실제 문서를 보기 전에 붙인 가상 출처라
    문서 종류(국세청안내 / 증권사FAQ) 조각이 상위 k에 있으면 적중으로 본다 (10/27, SCRUM-31 이후)
  - 코퍼스에 없는 정답(국세청 해석 서면-2022-국제세원-0764 등)만 있는 질문은 '답할 수 없음'으로 따로 센다
지표: hit@k(정답 하나라도 상위 k) · recall@k(정답 중 찾은 비율) · MRR · 재작성 횟수 · 약함(weak) 판정 수
--judge: answer@k — 상위 k 조각 중 '질문에 직접 답하는' 조각이 있는가를 LLM이 판정 (context relevance).
  10/27 첫 실측에서 실패의 대부분이 국세청 책자 쪽을 가져온 경우였다 — 시험지 정답이 법 조문만 적혀 있어 같은 내용을 설명한
  안내서 쪽이 오답으로 처리됐다. 조문 번호 일치(hit@k)와 답이 되는가(answer@k)를 함께 본다.
  조각(공개 문서)과 질문(합성)만 보낸다. 판정은 results/retrieval_judge_cache.json에 캐시 — 같은 (질문, 조각)은 다시 묻지 않는다.
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


JUDGE_PROMPT = """너는 해외주식 양도소득세 상담 검색 품질 평가자다. 고객 질문과 검색된 규정 조각들이 있다.
각 조각이 이 질문에 답하는 근거로 쓸 수 있는지 판정하라.
2 = 질문에 직접 답하는 내용이 있다 (상담원이 이 조각만 보고 답할 수 있음)
1 = 관련은 있지만 답은 아니다 (같은 주제, 다른 쟁점)
0 = 무관
반드시 JSON 배열만 출력: [{{"id": "조각id", "score": 0|1|2}}, ...]

질문: {q}

조각:
{chunks}"""


class Judge:
    """상위 k 조각의 관련도 판정. (case_id, chunk_id) 단위 캐시."""

    def __init__(self, model: str):
        from anthropic import Anthropic
        self.client, self.model = Anthropic(), model
        self.path = ROOT / "results" / "retrieval_judge_cache.json"
        self.cache: dict[str, int] = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        self.calls = self.tin = self.tout = 0

    def scores(self, case: dict, chunks: list[dict]) -> list[int]:
        key = lambda ch: f"{case['case_id']}|{ch['chunk_id']}"  # noqa: E731
        todo = [ch for ch in chunks if key(ch) not in self.cache]
        if todo:
            body = "\n\n".join(f"[{ch['chunk_id']}] {ch['law']} {ch['article']}\n{ch['text'][:900]}" for ch in todo)
            msg = self.client.messages.create(model=self.model, max_tokens=300,
                                              messages=[{"role": "user", "content": JUDGE_PROMPT.format(q=case["text"], chunks=body)}])
            self.calls += 1; self.tin += msg.usage.input_tokens; self.tout += msg.usage.output_tokens
            text = "".join(b.text for b in msg.content if b.type == "text")
            import re
            m = re.search(r"\[.*\]", text, re.S)
            got = {str(x.get("id")): int(x.get("score", 0)) for x in (json.loads(m.group(0)) if m else []) if isinstance(x, dict)}
            for ch in todo:
                self.cache[key(ch)] = got.get(ch["chunk_id"], 0)
        return [self.cache[key(ch)] for ch in chunks]

    def save(self) -> None:
        self.path.parent.mkdir(exist_ok=True)
        self.path.write_text(json.dumps(self.cache, ensure_ascii=False), encoding="utf-8")


CUTS = (1, 3, 5, 10, 20)


def evaluate(searcher, cases: list[dict], k: int, mode: str, rewrite: bool, judge: "Judge | None" = None, expand: bool = False) -> dict:
    rows, unanswerable = [], []
    for c in cases:
        tg = targets(c["expected"]["articles"], searcher.chunks)
        if not tg:
            unanswerable.append(c["case_id"]); continue
        t0 = time.perf_counter()
        from app.rag.search import dict_rewrite
        q = dict_rewrite(c["text"]) if expand else c["text"]
        r = searcher.search(q, k=k, mode=mode, rewrite=rewrite)
        ms = (time.perf_counter() - t0) * 1000
        got = [keys_of(ch) for ch in r.chunks]
        first = next((i for i, g in enumerate(got, 1) if g & set(tg)), None)
        found = {t for t in tg if any(t in g for g in got)}
        js = judge.scores(c, r.chunks) if judge else None
        first_answer = next((i for i, v in enumerate(js, 1) if v == 2), None) if js else None
        rows.append({"case_id": c["case_id"], "hit": first is not None, "rr": 1 / first if first else 0.0, "judge": js,
                     "answer": (max(js) == 2) if js else None, "first_answer": first_answer, "relevant": (max(js) >= 1) if js else None,
                     "recall": len(found) / len(tg), "weak": r.weak, "rewritten": r.rewritten_query, "ms": ms,
                     "top_vec": r.top_vec, "text": c["text"], "want": tg, "got": [f"{ch['law']} {ch['article']}" for ch in r.chunks]})
    n = len(rows)
    judged = [r for r in rows if r["judge"] is not None]
    curve = {j: sum(1 for r in judged if r["first_answer"] and r["first_answer"] <= j) / len(judged) for j in CUTS if j <= k} if judged else {}
    return {"mode": mode + ("+재작성" if rewrite else "") + ("+사전확장" if expand else ""), "n": n, "unanswerable": unanswerable, "curve": curve,
            "answer": (sum(r["answer"] for r in judged) / len(judged)) if judged else None,
            "relevant": (sum(r["relevant"] for r in judged) / len(judged)) if judged else None,
            "hit": sum(r["hit"] for r in rows) / n, "recall": statistics.mean(r["recall"] for r in rows),
            "mrr": statistics.mean(r["rr"] for r in rows), "weak": sum(r["weak"] for r in rows),
            "rewritten": sum(1 for r in rows if r["rewritten"]), "ms_p50": statistics.median(r["ms"] for r in rows), "rows": rows}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=3)
    ap.add_argument("--rewrite", choices=["dict", "llm"], default="dict")
    ap.add_argument("--cases", default=str(ROOT / "eval" / "cases.jsonl"))
    ap.add_argument("--show-misses", type=int, default=12)
    ap.add_argument("--judge", action="store_true", help="answer@k를 LLM으로 판정 (ANTHROPIC_API_KEY, 약 $0.3)")
    ap.add_argument("--judge-model", default="claude-sonnet-5-5")
    ap.add_argument("--modes", default="keyword,vector,hybrid,hybrid+rewrite,hybrid+expand",
                    help="비교할 방식(쉼표). 상위 20 판정은 비용이 커서 --k 20 --modes hybrid 처럼 좁혀서")
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
    spec = {"keyword": ("keyword", False, False), "vector": ("vector", False, False), "hybrid": ("hybrid", False, False),
            "hybrid+rewrite": ("hybrid", True, False), "hybrid+expand": ("hybrid", False, True), "keyword+expand": ("keyword", False, True)}
    want = [m.strip() for m in args.modes.split(",") if m.strip()]
    if not has_vec:
        want = [m for m in want if m.startswith("keyword")] or ["keyword", "keyword+expand"]
    configs = [spec[m] for m in want]
    judge = Judge(args.judge_model) if args.judge else None
    try:
        results = [evaluate(s, cases, args.k, m, rw, judge, ex) for m, rw, ex in configs]
    finally:
        if judge:
            judge.save()
    jcol = f" answer@{args.k} | 관련@{args.k} |" if judge else ""
    print(f"| 방식 | n | hit@{args.k} | recall@{args.k} | MRR |{jcol} 약함 판정 | 재작성 | 지연 p50 |")
    print("| --- " * (8 + (2 if judge else 0)) + "|")
    for r in results:
        jv = f" {r['answer']:.0%} | {r['relevant']:.0%} |" if judge else ""
        print(f"| {r['mode']} | {r['n']} | {r['hit']:.0%} | {r['recall']:.0%} | {r['mrr']:.2f} |{jv} {r['weak']} | {r['rewritten']} | {r['ms_p50']:.0f}ms |")
    if judge:
        price = {"claude-sonnet-5-5": (2, 10), "claude-haiku-5-5": (0.2, 1)}.get(args.judge_model, (0, 0))
        cuts = [j for j in CUTS if j <= args.k]
        if args.k > 3:
            print(f"\n| 방식 | " + " | ".join(f"answer@{j}" for j in cuts) + " |")
            print("| --- " * (len(cuts) + 1) + "|")
            for r in results:
                print(f"| {r['mode']} | " + " | ".join(f"{r['curve'][j]:.0%}" for j in cuts) + " |")
            print("읽는 법: answer@20이 높고 @3이 낮으면 순위 문제(재정렬로 해결), @20도 낮으면 내용 문제(코퍼스 보강)")
        print(f"\n판정 {judge.calls}회 (캐시 {len(judge.cache)}쌍) · 토큰 {judge.tin:,}/{judge.tout:,} ≈ ${judge.tin / 1e6 * price[0] + judge.tout / 1e6 * price[1]:.2f}")
        # 조문 번호는 틀렸는데 답은 되는 경우 = 시험지 정답 라벨의 한계
        best = results[-1]
        mislabeled = [r for r in best["rows"] if not r["hit"] and r["answer"]]
        print(f"'{best['mode']}': 정답 조문은 아니지만 답이 되는 조각을 가져온 경우 {len(mislabeled)}건 / 실패 {sum(not r['hit'] for r in best['rows'])}건")
    print(f"\n답할 수 없음(정답이 코퍼스에 없음): {len(results[0]['unanswerable'])}건 {results[0]['unanswerable']}")
    best = results[-1]
    if has_vec:  # WEAK_COS 보정 입력: 적중/실패별 벡터 1위 코사인
        hyb = next((r for r in results if r["mode"] == "hybrid"), results[-1])
        hv = [r["top_vec"] for r in hyb["rows"] if r["hit"] and r["top_vec"] is not None]
        mv = [r["top_vec"] for r in hyb["rows"] if not r["hit"] and r["top_vec"] is not None]
        if hv and mv:
            print(f"벡터 1위 코사인 — 적중 평균 {statistics.mean(hv):.3f} (최소 {min(hv):.3f}) · 실패 평균 {statistics.mean(mv):.3f} (최대 {max(mv):.3f})")
    misses = [r for r in best["rows"] if (not r["answer"]) if r["answer"] is not None] if judge else [r for r in best["rows"] if not r["hit"]]
    print(f"\n## '{best['mode']}' {'답이 되는 조각 없음' if judge else '실패'} {len(misses)}건 (상위 {args.show_misses})")
    for r in misses[: args.show_misses]:
        print(f"- {r['case_id']} {r['text'][:50]}\n  정답 {[t[1] for t in r['want']]} · 상위 {r['got']}" + (f"\n  재작성: {r['rewritten']}" if r["rewritten"] else ""))
    out = ROOT / "results" / f"retrieval_k{args.k}_{meta['version'] if meta else 'kw'}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps([{k: v for k, v in r.items()} for r in results], ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"\n저장: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
