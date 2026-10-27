"""F06 RAG (SCRUM-32·33). 임베더는 HashEmbedder(결정론, 의미 없음) — 배선·버전·순위 규칙만 본다. 실제 품질은 맥에서 bge-m3로."""
import asyncio

import pytest
from fastapi.testclient import TestClient

from app import main, repo
from app.rag import embed, keyword, search, store
from app.tools import registry

CHUNKS = [
    {"chunk_id": "a", "law": "소득세법", "article": "제118조의4", "title": "국외자산 양도소득의 필요경비 계산", "doc_type": "법령",
     "text": "취득가액 해당 자산의 취득에 든 실지거래가액"},
    {"chunk_id": "b", "law": "소득세법 시행령", "article": "제178조의5", "title": "국외자산 양도차익의 외화환산", "doc_type": "시행령",
     "text": "양도가액 및 필요경비를 수령하거나 지출한 날 현재 외국환거래법에 의한 기준환율 또는 재정환율에 의하여 계산한다"},
    {"chunk_id": "c", "law": "소득세법", "article": "제103조", "title": "양도소득 기본공제", "doc_type": "법령",
     "text": "양도소득이 있는 거주자에 대해서는 각 호의 소득별로 해당 과세기간의 양도소득금액에서 각각 연 250만원을 공제한다"},
    {"chunk_id": "d", "law": "키움증권 RIA 설명서", "article": "대체입고 Q&A", "doc_type": "증권사FAQ",
     "text": "타사 해외주식 대체입고 시 입고 전 증권사가 이동평균법으로 산출한 양도소득금액과 선입선출법 기준 양도소득금액 간에 차이가 발생할 수 있습니다"},
]


def make(tmp_path, chunks=CHUNKS):
    st = store.LocalStore(tmp_path / "idx")
    emb = embed.HashEmbedder()
    st.write(chunks, emb.embed([c["text"] for c in chunks]), {"model": emb.name, "dim": emb.dim})
    return st, emb


# ---------- SCRUM-32 ----------

def test_self_is_top1(tmp_path):
    st, emb = make(tmp_path)
    for i, c in enumerate(CHUNKS):
        assert st.search(emb.embed([c["text"]])[0], 3)[0][0] == i  # 수용 조건: 자기 자신이 1위


def test_reindex_bumps_version_and_keeps_old(tmp_path):
    st, emb = make(tmp_path)
    assert st.active()["version"] == "v1" and st.active()["n_chunks"] == 4 and st.active()["model"] == "hash-256"
    st.write(CHUNKS[:2], emb.embed([c["text"] for c in CHUNKS[:2]]), {"model": emb.name, "dim": emb.dim})
    assert st.active()["version"] == "v2" and len(st.chunks()) == 2
    assert (tmp_path / "idx" / "v1" / "vectors.npy").exists()  # 이전 버전 벡터는 남는다


def test_model_mismatch_refuses(tmp_path, monkeypatch):
    make(tmp_path)
    monkeypatch.setenv("RAG_INDEX_DIR", str(tmp_path / "idx"))
    monkeypatch.setenv("EMBEDDER", "ollama")  # 색인은 hash-256으로 만들었는데 bge-m3로 질의하려 함
    store.reset_store(); search.reset_searcher()
    with pytest.raises(RuntimeError, match="재색인"):
        search.get_searcher()


def test_index_script_refuses_placeholder_corpus(monkeypatch, tmp_path):
    import importlib.util
    from pathlib import Path
    monkeypatch.setattr(repo, "load_corpus", lambda: [{"law": "x", "article": "y", "text": "★원문 확인 필요★"}])
    spec = importlib.util.spec_from_file_location("ic", Path(repo.DATA).parent / "scripts" / "index_corpus.py")
    ic = importlib.util.module_from_spec(spec); spec.loader.exec_module(ic)
    with pytest.raises(SystemExit, match="자리표시"):
        ic.run()


def test_admin_reindex_requires_token(monkeypatch):
    c = TestClient(main.app)
    assert c.post("/v1/admin/corpus/reindex").status_code == 403
    monkeypatch.setenv("ADMIN_TOKEN", "s3cret")
    assert c.post("/v1/admin/corpus/reindex", headers={"X-Admin-Token": "wrong"}).status_code == 403


# ---------- SCRUM-33 ----------

def test_article_number_normalization():
    assert keyword.norm_articles("§118의4 와 118조의4, 제118조의4") == ["제118조의4"] * 3
    assert keyword.norm_articles("§103 제103조") == ["제103조", "제103조"]


def test_article_query_hits_by_keyword():
    s = search.HybridSearcher(CHUNKS)  # 벡터 없이 키워드만
    r = s.search("§118의4", k=3, mode="keyword")
    assert r.chunks[0]["article"] == "제118조의4"  # SCRUM-33: 조문 번호는 키워드로 1위


def test_hybrid_fuses_and_reports_scores(tmp_path):
    st, emb = make(tmp_path)
    s = search.HybridSearcher(CHUNKS, st, emb, weak_cos=0.0)
    r = s.search("대체입고 양도소득금액 차이", k=3)
    assert r.chunks[0]["chunk_id"] == "d" and r.chunks[0]["vec_score"] is not None and r.chunks[0]["kw_score"] is not None
    assert not r.weak and r.rewritten_query is None


def test_weak_query_rewritten_by_dictionary(tmp_path):
    st, emb = make(tmp_path)
    s = search.HybridSearcher(CHUNKS, st, emb, weak_cos=0.99)  # 일부러 높게 → 첫 검색은 약함
    r = s.search("환율 언제 기준이에요", k=3)
    assert r.rewritten_query and "기준환율" in r.rewritten_query and r.rewrite_by == "dict"
    assert any(c["article"] == "제178조의5" for c in r.chunks)  # 수용 조건: 시행령 조문 포함


def test_category_prefers_doc_types():
    s = search.HybridSearcher(CHUNKS)
    neutral = [c["chunk_id"] for c in s.search("양도소득금액", k=4, mode="keyword").chunks]
    multi = [c["chunk_id"] for c in s.search("양도소득금액", k=4, category="MULTI_BROKER", mode="keyword").chunks]
    assert multi.index("d") <= neutral.index("d")  # 증권사 안내 가중


def test_registry_tool_uses_hybrid_and_fills_ctx(tmp_path, monkeypatch):
    st, emb = make(tmp_path)
    monkeypatch.setattr(search, "_searcher", search.HybridSearcher(CHUNKS, st, emb, weak_cos=0.0))
    ctx = registry.ToolContext(1, 2025)
    ctx.category = "TRANSFER_BASIS"
    out = asyncio.run(registry.invoke("reg_search", {"query": "대체입고 취득가액"}, ctx))
    assert set(out) == {"chunks", "rewritten_query", "rewrite_by", "weak"}
    assert out["chunks"] and ctx.chunks == out["chunks"]
    assert {c["article"] for c in out["chunks"][:3]} & {"제118조의4", "대체입고 Q&A"}  # 수용 조건: 재작성 없이 상위 3
    assert out["rewritten_query"] is None


def test_no_index_falls_back_to_keyword_over_corpus():
    s = search.get_searcher()
    assert s.store is None and len(s.chunks) == len(repo.load_corpus())


def test_retrieval_judge_answer_at_k_and_cache(tmp_path, monkeypatch):
    """answer@k: 조문 번호가 틀려도 답이 되는 조각을 가져오면 answer=True. 같은 (질문, 조각)은 다시 묻지 않는다."""
    import json as _j
    import eval.retrieval as R

    class Msg:
        def __init__(self, text):
            self.content = [type("B", (), {"type": "text", "text": text})()]
            self.usage = type("U", (), {"input_tokens": 10, "output_tokens": 5})()

    class FakeClient:
        calls = 0
        class messages:  # noqa: N801
            @staticmethod
            def create(**kw):
                FakeClient.calls += 1
                ids = [l.split("]")[0][1:] for l in kw["messages"][0]["content"].split("\n") if l.startswith("[")]
                return Msg(_j.dumps([{"id": i, "score": 2 if i == "d" else 0} for i in ids]))

    j = R.Judge.__new__(R.Judge)
    j.client, j.model, j.path, j.cache, j.calls, j.tin, j.tout = FakeClient, "fake", tmp_path / "c.json", {}, 0, 0, 0
    case = {"case_id": "X1", "text": "대체입고 양도소득금액 차이", "expected": {"articles": [["소득세법", "제118조의4"]]}}
    s = search.HybridSearcher(CHUNKS)
    r1 = R.evaluate(s, [case], 1, "keyword", False, j)  # 1위는 키움 Q&A(d) — 조문 라벨과 다름
    assert r1["rows"][0]["hit"] is False and r1["rows"][0]["answer"] is True and r1["answer"] == 1.0  # 라벨은 틀려도 답은 됨
    R.evaluate(s, [case], 1, "keyword", False, j)
    assert FakeClient.calls == 1  # 캐시
    j.save()
    assert (tmp_path / "c.json").exists()
