"""F06-4 벡터 저장 + 코퍼스 버전 (SCRUM-32).

LocalStore (기본, RAG_STORE=local): data/index/<version>/{vectors.npy, chunks.json, meta.json}, ACTIVE 파일이 현재 버전.
  DB 없이 돌아가고 159조각이면 전수 코사인으로 충분히 빠르다. 평가·데모 기본.
PgStore (RAG_STORE=pg): db/ai_schema.sql의 reg_chunk·corpus_meta (pgvector). 운영·S2 구성. PG_DSN 필요.

재색인하면 새 버전(v1 → v2)으로 쓰고 이전 버전 벡터는 지우지 않는다 → 평가 결과를 어느 버전으로 냈는지 재현 가능.
meta: version, model, dim, corpus_hash, n_chunks, created_at, embed_seconds_per_100
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

import numpy as np

ROOT = Path(__file__).resolve().parents[2]


class VectorStore(Protocol):
    def write(self, chunks: list[dict], vectors: list[list[float]], meta: dict) -> str: ...
    def active(self) -> dict | None: ...
    def chunks(self) -> list[dict]: ...
    def search(self, vec: list[float], k: int) -> list[tuple[int, float]]: ...


class LocalStore:
    def __init__(self, base: str | Path | None = None):
        self.base = Path(base or os.getenv("RAG_INDEX_DIR", ROOT / "data" / "index"))
        self._cache: tuple[str, np.ndarray, list[dict], dict] | None = None

    def _versions(self) -> list[str]:
        return sorted((p.name for p in self.base.glob("v*") if (p / "meta.json").exists()), key=lambda s: int(s[1:]))

    def write(self, chunks: list[dict], vectors: list[list[float]], meta: dict) -> str:
        vs = self._versions()
        version = f"v{int(vs[-1][1:]) + 1 if vs else 1}"
        d = self.base / version
        d.mkdir(parents=True, exist_ok=True)
        np.save(d / "vectors.npy", np.asarray(vectors, dtype=np.float32))
        (d / "chunks.json").write_text(json.dumps(chunks, ensure_ascii=False), encoding="utf-8")
        meta = {**meta, "version": version, "n_chunks": len(chunks), "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
        (self.base / "ACTIVE").write_text(version, encoding="utf-8")
        self._cache = None
        return version

    def _load(self):
        ap = self.base / "ACTIVE"
        if not ap.exists():
            return None
        v = ap.read_text(encoding="utf-8").strip()
        if self._cache and self._cache[0] == v:
            return self._cache
        d = self.base / v
        self._cache = (v, np.load(d / "vectors.npy"), json.loads((d / "chunks.json").read_text(encoding="utf-8")),
                       json.loads((d / "meta.json").read_text(encoding="utf-8")))
        return self._cache

    def active(self) -> dict | None:
        c = self._load()
        return c[3] if c else None

    def chunks(self) -> list[dict]:
        c = self._load()
        return c[2] if c else []

    def search(self, vec: list[float], k: int) -> list[tuple[int, float]]:
        c = self._load()
        if c is None:
            return []
        sims = c[1] @ np.asarray(vec, dtype=np.float32)  # 둘 다 정규화 → 코사인
        idx = np.argsort(-sims)[:k]
        return [(int(i), float(sims[i])) for i in idx]


class PgStore:
    """pgvector. psycopg·pgvector 패키지가 있을 때만. 스키마는 db/ai_schema.sql."""

    def __init__(self, dsn: str | None = None):
        import psycopg  # noqa: F401 — 없으면 ImportError로 바로 알린다
        from pgvector.psycopg import register_vector
        self.dsn = dsn or os.environ["PG_DSN"]
        self._register = register_vector
        self._chunks: list[dict] | None = None

    def _conn(self):
        import psycopg
        conn = psycopg.connect(self.dsn, autocommit=True)
        self._register(conn)
        return conn

    def write(self, chunks, vectors, meta) -> str:
        with self._conn() as conn:
            n = conn.execute("SELECT count(*) FROM corpus_meta").fetchone()[0]
            version = f"v{n + 1}"
            conn.execute("INSERT INTO corpus_meta(version, model, dim, corpus_hash, n_chunks, embed_seconds_per_100) VALUES (%s,%s,%s,%s,%s,%s)",
                         (version, meta["model"], meta["dim"], meta.get("corpus_hash"), len(chunks), meta.get("embed_seconds_per_100")))
            with conn.cursor() as cur:
                for c, v in zip(chunks, vectors):
                    cur.execute("""INSERT INTO reg_chunk(corpus_version, chunk_id, law, article, clause, title, text, embedding, effective_date,
                                   source_url, doc_type, tax_year_scope, keywords) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                                (version, c["chunk_id"], c["law"], c["article"], c.get("clause"), c.get("title"), c["text"], np.asarray(v),
                                 c.get("effective_date"), c.get("source_url"), c.get("doc_type"), c.get("tax_year_scope"), c.get("keywords") or []))
            conn.execute("UPDATE corpus_meta SET active = (version = %s)", (version,))
        self._chunks = None
        return version

    def active(self) -> dict | None:
        with self._conn() as conn:
            r = conn.execute("SELECT version, model, dim, corpus_hash, n_chunks, created_at FROM corpus_meta WHERE active").fetchone()
        return dict(zip(["version", "model", "dim", "corpus_hash", "n_chunks", "created_at"], r)) if r else None

    def chunks(self) -> list[dict]:
        if self._chunks is None:
            with self._conn() as conn:
                rows = conn.execute("""SELECT chunk_id, law, article, clause, title, text, effective_date, source_url, doc_type, tax_year_scope, keywords
                                       FROM reg_chunk WHERE corpus_version = (SELECT version FROM corpus_meta WHERE active) ORDER BY id""").fetchall()
            cols = ["chunk_id", "law", "article", "clause", "title", "text", "effective_date", "source_url", "doc_type", "tax_year_scope", "keywords"]
            self._chunks = [dict(zip(cols, r)) for r in rows]
        return self._chunks

    def search(self, vec, k):
        ids = {c["chunk_id"]: i for i, c in enumerate(self.chunks())}
        with self._conn() as conn:
            rows = conn.execute("""SELECT chunk_id, 1 - (embedding <=> %s) FROM reg_chunk
                                   WHERE corpus_version = (SELECT version FROM corpus_meta WHERE active)
                                   ORDER BY embedding <=> %s LIMIT %s""", (np.asarray(vec), np.asarray(vec), k)).fetchall()
        return [(ids[cid], float(s)) for cid, s in rows if cid in ids]


_store: VectorStore | None = None


def get_store() -> VectorStore:
    global _store
    if _store is None:
        _store = PgStore() if os.getenv("RAG_STORE", "local") == "pg" else LocalStore()
    return _store


def reset_store() -> None:
    global _store
    _store = None
