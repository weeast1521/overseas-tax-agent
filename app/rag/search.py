"""F06-5~8 하이브리드 검색 (SCRUM-33).

  vector(bge-m3 코사인) 순위 + keyword(BM25 2-gram) 순위 → RRF(k=60) → 유형별 문서 가중 → 상위 k
  약하면(벡터 1위 코사인 < WEAK_COS) 질문을 법령 용어로 바꿔 1회 재검색: 사전 치환 → 그래도 약하면 LLM (REWRITE=llm)

색인(store)이 없으면 키워드만으로 동작한다 — 세션·CI에서 Ollama 없이도 돈다(SCRUM-32 이전의 스텁 대체).
결과의 vec_score·kw_score·rewritten_query·weak는 trace에 남아 "왜 이 조문이 나왔나"를 설명한다.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

from app.rag.keyword import BM25

RRF_K = 60

# F06-8 유형별로 먼저 볼 문서. 엄격한 필터 대신 가중(RRF에 보너스) — 조각 159개라 걸러 내면 남는 게 너무 적다.
PREFERRED_DOCS: dict[str, set[str]] = {
    "TRANSFER_BASIS": {"법령", "증권사FAQ", "국세청안내"},
    "MULTI_BROKER": {"국세청안내", "증권사FAQ", "법령"},
    "TAX_BASIS": {"법령", "시행령", "국세청안내"},
    "FX_BASIS": {"법령", "시행령", "국세청안내"},
    "OFFSET": {"법령", "국세청안내"},
    "FILING": {"국세청안내", "법령"},
}
PREF_BONUS = 0.5 / (RRF_K + 1)  # 1위 RRF 점수의 절반 — 같은 순위대면 선호 문서가 앞선다

# 구어 → 법령 용어 (재작성 1단계). 법령은 '결제일'·'대체입고'라는 말을 쓰지 않는다 (SCRUM-31에서 확인)
SYNONYMS: list[tuple[str, str]] = [
    (r"환율|달러.{0,4}원화|환산", "외화 환산 기준환율 양도가액 필요경비를 수령하거나 지출한 날"),
    (r"옮[기겨긴]|이체|이관|대체입고|넘[기겨]|이전", "대체입고 이관 취득가액 실지거래가액 이동평균 선입선출"),
    (r"공제|250", "양도소득 기본공제 250만원"),
    (r"손실|손해|마이너스|상계|통산", "양도차손 손익 통산 양도소득금액"),
    (r"합[쳐치산]|두 군데|여러 증권사|두 증권사|타사", "합산 신고 타사 거래 양도소득금액"),
    (r"언제까지|기한|마감|신고.{0,3}언제", "확정신고 기한 다음 연도 5월"),
    (r"세율|몇 ?%|퍼센트", "세율 100분의 20 지방소득세"),
    (r"수수료|비용", "양도비 필요경비"),
    (r"평균|선입선출|먼저 산|방식", "취득가액 산정 이동평균 선입선출 먼저 취득한 것부터 양도"),
]


@dataclass
class SearchResult:
    chunks: list[dict]
    weak: bool
    rewritten_query: str | None = None
    rewrite_by: str | None = None  # dict | llm
    top_vec: float | None = None
    debug: dict = field(default_factory=dict)

    def as_tool(self) -> dict:
        keep = ("chunk_id", "law", "article", "clause", "title", "doc_type", "effective_date", "source_url", "text", "score", "vec_score", "kw_score")
        return {"chunks": [{k: c.get(k) for k in keep} for c in self.chunks], "rewritten_query": self.rewritten_query,
                "rewrite_by": self.rewrite_by, "weak": self.weak}


class HybridSearcher:
    def __init__(self, chunks: list[dict], store=None, embedder=None, weak_cos: float | None = None):
        self.chunks = chunks
        self.store, self.embedder = store, embedder
        self.bm25 = BM25([f"{c.get('law', '')} {c.get('article', '')} {c.get('title', '')} {c['text']}" for c in chunks])
        self.weak_cos = weak_cos if weak_cos is not None else float(os.getenv("WEAK_COS", "0.5"))

    def _once(self, query: str, k: int, category: str | None, mode: str) -> tuple[list[dict], float | None]:
        pool = max(20, k * 4)
        vec = []
        if mode in ("hybrid", "vector") and self.store is not None and self.embedder is not None:
            vec = self.store.search(self.embedder.embed([query])[0], pool)
        kw = self.bm25.search(query, pool) if mode in ("hybrid", "keyword") else []
        fused: dict[int, dict] = {}
        for rank, (i, s) in enumerate(vec, 1):
            fused.setdefault(i, {"rrf": 0.0})["rrf"] += 1 / (RRF_K + rank)
            fused[i]["vec_score"] = round(s, 4)
        for rank, (i, s) in enumerate(kw, 1):
            fused.setdefault(i, {"rrf": 0.0})["rrf"] += 1 / (RRF_K + rank)
            fused[i]["kw_score"] = round(s, 3)
        pref = PREFERRED_DOCS.get(category or "", set())
        for i, f in fused.items():
            if self.chunks[i].get("doc_type") in pref:
                f["rrf"] += PREF_BONUS
        top = sorted(fused.items(), key=lambda x: -x[1]["rrf"])[:k]
        out = [{**self.chunks[i], "score": round(f["rrf"], 5), "vec_score": f.get("vec_score"), "kw_score": f.get("kw_score")} for i, f in top]
        return out, (vec[0][1] if vec else None)

    def is_weak(self, results: list[dict], top_vec: float | None) -> bool:
        """F06-6. 결과가 없거나, 벡터 1위 코사인이 기준 미만. 벡터가 없으면(키워드만) 키워드 점수가 전부 없을 때만."""
        if not results:
            return True
        if top_vec is not None:
            return top_vec < self.weak_cos
        return all(r.get("kw_score") is None for r in results)

    def search(self, query: str, k: int = 5, category: str | None = None, mode: str = "hybrid", rewrite: bool = True) -> SearchResult:
        res, top = self._once(query, k, category, mode)
        weak = self.is_weak(res, top)
        if not (weak and rewrite):
            return SearchResult(res, weak, top_vec=top)
        # F06-7 재작성 1회: 사전 → 그래도 약하면 LLM
        q2 = dict_rewrite(query)
        by = "dict"
        if q2 == query or os.getenv("REWRITE", "dict") == "llm":
            q_llm = llm_rewrite(query)
            if q_llm:
                q2, by = q_llm, "llm"
        if q2 == query:
            return SearchResult(res, weak, top_vec=top)
        res2, top2 = self._once(q2, k, category, mode)
        return SearchResult(res2, self.is_weak(res2, top2), rewritten_query=q2, rewrite_by=by, top_vec=top2,
                            debug={"first_top_vec": top})


def dict_rewrite(query: str) -> str:
    adds = [rep for pat, rep in SYNONYMS if re.search(pat, query)]
    return f"{query} {' '.join(adds)}" if adds else query


def llm_rewrite(query: str) -> str | None:
    """LLM에게 법령 용어로 바꾸게 한다. REWRITE=llm일 때만. 폐쇄망(LLM_PROVIDER=ollama)이면 로컬 모델. 실패하면 None."""
    if os.getenv("REWRITE", "dict") != "llm":
        return None
    prompt = ("해외주식 양도소득세 민원 문장을 법령·국세청 안내에서 쓰는 용어로 바꿔 검색어 한 줄만 출력하라. 설명 금지.\n"
              "예) 환율 언제 기준이에요 → 양도가액 외화 환산 기준환율 수령한 날\n"
              "예) 다른 증권사에서 옮겨온 주식 → 대체입고 주식 취득가액 실지거래가액\n"
              "예) 손해 본 거랑 퉁쳐지나요 → 양도차손 손익 통산\n"
              f"문장: {query}\n검색어:")
    try:
        if os.getenv("LLM_PROVIDER", "claude") == "ollama":
            import urllib.request
            body = json.dumps({"model": os.getenv("OLLAMA_CHAT_MODEL", "qwen2.5:7b"), "prompt": prompt, "stream": False}).encode()
            req = urllib.request.Request(f"{os.getenv('OLLAMA_URL', 'http://localhost:11434')}/api/generate", data=body,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310
                text = json.loads(r.read())["response"]
        else:
            from anthropic import Anthropic
            msg = Anthropic().messages.create(model=os.getenv("REWRITE_MODEL", "claude-haiku-5-5"), max_tokens=80,
                                              messages=[{"role": "user", "content": prompt}])
            text = "".join(b.text for b in msg.content if b.type == "text")
        line = text.strip().splitlines()[0].strip() if text.strip() else ""
        return line[:200] or None
    except Exception:
        return None


_searcher: HybridSearcher | None = None


def get_searcher() -> HybridSearcher:
    """색인이 있으면 하이브리드, 없으면 코퍼스 파일로 키워드만."""
    global _searcher
    if _searcher is None:
        from app import repo
        from app.rag import embed, store
        st = store.get_store()
        meta = st.active() if os.getenv("RAG_STORE", "local") != "none" else None
        if meta:
            emb = embed.get_embedder()
            if emb.name != meta.get("model"):  # 다른 모델로 만든 벡터와 섞이면 검색이 조용히 망가진다
                raise RuntimeError(f"색인 {meta.get('version')}은 {meta.get('model')}로 만들었는데 지금 임베더는 {emb.name} — 재색인하거나 EMBEDDER를 맞출 것")
            _searcher = HybridSearcher(st.chunks(), st, emb)
        else:
            _searcher = HybridSearcher(repo.load_corpus())
    return _searcher


def reset_searcher() -> None:
    global _searcher
    _searcher = None
