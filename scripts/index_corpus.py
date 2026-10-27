"""SCRUM-32 코퍼스 → 임베딩 → 저장소(새 버전). 맥에서 Ollama bge-m3로.

  ollama pull bge-m3
  python scripts/index_corpus.py                 # 로컬 파일 저장소 data/index/vN
  RAG_STORE=pg PG_DSN=... python scripts/index_corpus.py   # pgvector

출력: 버전, 조각 수, 100조각당 임베딩 시간(수용 조건). 같은 코퍼스로 다시 돌리면 새 버전이 생기고 이전 버전은 남는다.
"""
from __future__ import annotations

import hashlib
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def corpus_hash(paths: list[Path]) -> str:
    h = hashlib.sha256()
    for p in sorted(paths):
        h.update(p.name.encode()); h.update(p.read_bytes())
    return h.hexdigest()[:12]


def run() -> dict:
    from app import repo
    from app.rag import embed, store
    chunks = repo.load_corpus()
    if any("★" in c["text"] for c in chunks):
        raise SystemExit("코퍼스에 ★ 자리표시가 있음 — scripts/build_corpus.py --build 먼저 (seed.json으로 색인하지 않는다)")
    emb = embed.get_embedder()
    texts = [f"{c['law']} {c['article']} {c.get('title') or ''}\n{c['text']}" for c in chunks]
    t0 = time.perf_counter()
    vecs = emb.embed(texts)
    sec = time.perf_counter() - t0
    meta = {"model": emb.name, "dim": emb.dim, "corpus_hash": corpus_hash(repo.corpus_files()),
            "corpus_files": [p.name for p in repo.corpus_files()], "embed_seconds_total": round(sec, 2),
            "embed_seconds_per_100": round(sec / max(1, len(chunks)) * 100, 2)}
    version = store.get_store().write(chunks, vecs, meta)
    return {**meta, "version": version, "n_chunks": len(chunks)}


if __name__ == "__main__":
    r = run()
    print(f"{r['version']}: {r['n_chunks']}조각 · {r['model']} {r['dim']}차원 · 임베딩 {r['embed_seconds_total']}초 "
          f"(100조각당 {r['embed_seconds_per_100']}초) · 코퍼스 {r['corpus_hash']}")
