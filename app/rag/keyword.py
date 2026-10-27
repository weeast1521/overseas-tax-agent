"""F06-5 키워드 검색 — BM25 over 글자 2-gram + 조문 번호 정규화 (SCRUM-33).

한국어는 띄어쓰기 단위 토큰으로는 '대체입고된'·'대체입고' 같은 활용형이 안 맞는다. 형태소 분석기 없이 글자 2-gram이면
충분히 잡히고 의존성이 없다(폐쇄망). 조문 번호는 '§118의4'·'118조의4'·'제118조의4'를 하나로 맞춰 통째로 토큰화한다 —
벡터 검색이 약한 부분(정확한 번호·전문 용어)을 키워드가 메운다.
"""
from __future__ import annotations

import math
import re
from collections import Counter

_ART = re.compile(r"(?:§\s*|제\s*)?(\d{1,4})\s*조?\s*의\s*(\d{1,3})|(?:§\s*|제\s*)(\d{1,4})\s*조?|(\d{1,4})\s*조")


def norm_articles(text: str) -> list[str]:
    """'§118의4' '118조의4' '제118조의4' → '제118조의4'. '제103조' '§103' → '제103조'."""
    out = []
    for m in _ART.finditer(text):
        if m.group(1):
            out.append(f"제{int(m.group(1))}조의{int(m.group(2))}")
        else:
            n = m.group(3) or m.group(4)
            out.append(f"제{int(n)}조")
    return out


def tokens(text: str) -> list[str]:
    arts = norm_articles(text)
    s = re.sub(r"[^0-9A-Za-z가-힣]", "", text)
    grams = [s[i:i + 2] for i in range(len(s) - 1)]
    return grams + [f"ART:{a}" for a in arts]


class BM25:
    def __init__(self, docs: list[str], k1: float = 1.2, b: float = 0.75):
        self.k1, self.b = k1, b
        self.docs = [Counter(tokens(d)) for d in docs]
        self.lens = [sum(d.values()) for d in self.docs]
        self.avg = sum(self.lens) / max(1, len(self.lens))
        df = Counter(t for d in self.docs for t in d)
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def search(self, query: str, k: int) -> list[tuple[int, float]]:
        q = Counter(tokens(query))
        scores = []
        for i, (d, ln) in enumerate(zip(self.docs, self.lens)):
            s = 0.0
            for t, qf in q.items():
                f = d.get(t)
                if not f:
                    continue
                w = 3.0 if t.startswith("ART:") else 1.0  # 조문 번호가 맞으면 크게
                s += w * self.idf.get(t, 0) * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * ln / self.avg))
            if s > 0:
                scores.append((i, s))
        return sorted(scores, key=lambda x: -x[1])[:k]
