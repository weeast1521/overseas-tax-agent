"""F06-3 임베딩 (SCRUM-32). 폐쇄망 전제라 외부 API를 쓰지 않는다 — 로컬 Ollama의 bge-m3(1024차원, 다국어).

  ollama pull bge-m3          # 맥에 한 번
  EMBEDDER=ollama (기본)       OLLAMA_URL=http://localhost:11434

EMBEDDER=hash는 테스트·세션용 결정론 임베더(글자 3-gram 해싱). 의미 검색 품질은 없다 — 배선 확인용.
모델이 바뀌면 모든 벡터가 무효이므로 name·dim을 저장소 메타에 같이 기록한다 (store.py).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import urllib.request
from typing import Protocol


class Embedder(Protocol):
    name: str
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


def _norm(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


class OllamaEmbedder:
    """POST {OLLAMA_URL}/api/embed {"model": "bge-m3", "input": [..]} → {"embeddings": [[..], ..]}"""

    def __init__(self, model: str | None = None, url: str | None = None, batch: int = 16):
        self.model = model or os.getenv("EMBED_MODEL", "bge-m3")
        self.url = (url or os.getenv("OLLAMA_URL", "http://localhost:11434")).rstrip("/")
        self.batch = batch
        self.name = self.model
        self.dim = 1024 if self.model.startswith("bge-m3") else 0  # 첫 호출 결과로 확정

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch):
            body = json.dumps({"model": self.model, "input": texts[i:i + self.batch]}).encode()
            req = urllib.request.Request(f"{self.url}/api/embed", data=body, headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=120) as r:  # noqa: S310 (로컬 Ollama)
                    vecs = json.loads(r.read())["embeddings"]
            except OSError as e:
                raise RuntimeError(f"Ollama 임베딩 실패({self.url}, {self.model}) — `ollama serve`와 `ollama pull {self.model}` 확인: {e}") from e
            out.extend(_norm(v) for v in vecs)
        if out:
            self.dim = len(out[0])
        return out


class HashEmbedder:
    """테스트용. 글자 3-gram을 해싱해 dim차원 벡터로. 같은 글자를 많이 공유하면 가깝다(어휘 유사도 수준)."""

    def __init__(self, dim: int = 256):
        self.name, self.dim = f"hash-{dim}", dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        vecs = []
        for t in texts:
            v = [0.0] * self.dim
            s = "".join(t.split())
            for i in range(max(1, len(s) - 2)):
                h = int(hashlib.md5(s[i:i + 3].encode()).hexdigest()[:8], 16)
                v[h % self.dim] += 1.0
            vecs.append(_norm(v))
        return vecs


def get_embedder() -> Embedder:
    kind = os.getenv("EMBEDDER", "ollama")
    return HashEmbedder() if kind == "hash" else OllamaEmbedder()
