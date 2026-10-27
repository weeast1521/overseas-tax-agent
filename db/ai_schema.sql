-- F06 규정 검색 AI DB (SCRUM-32). PostgreSQL 16 + pgvector. RAG_STORE=pg 일 때 app/rag/store.py PgStore가 쓴다.
-- 재색인 = 새 corpus_version 행 + 새 reg_chunk 행들. 예전 버전 벡터는 지우지 않는다(평가 재현).
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS corpus_meta (
  version               TEXT PRIMARY KEY,           -- v1, v2 …
  model                 TEXT NOT NULL,              -- bge-m3
  dim                   INTEGER NOT NULL,           -- 1024
  corpus_hash           TEXT,                       -- data/corpus/v1 파일 해시
  n_chunks              INTEGER NOT NULL,
  embed_seconds_per_100 REAL,                       -- 수용 조건: M2 Pro 100 chunk 임베딩 시간
  active                BOOLEAN NOT NULL DEFAULT FALSE,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS reg_chunk (
  id              BIGSERIAL PRIMARY KEY,
  corpus_version  TEXT NOT NULL REFERENCES corpus_meta(version),
  chunk_id        TEXT NOT NULL,
  law             TEXT NOT NULL,
  article         TEXT NOT NULL,
  clause          TEXT,
  title           TEXT,
  text            TEXT NOT NULL,
  embedding       vector(1024) NOT NULL,
  effective_date  DATE,
  source_url      TEXT,
  doc_type        TEXT,
  tax_year_scope  TEXT,
  keywords        TEXT[],
  UNIQUE (corpus_version, chunk_id)
);
-- 조각 수가 수백 개라 전수 탐색으로도 충분하지만, 늘어날 때를 위해 HNSW (코사인)
CREATE INDEX IF NOT EXISTS ix_reg_chunk_hnsw ON reg_chunk USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS ix_reg_chunk_version ON reg_chunk (corpus_version);
