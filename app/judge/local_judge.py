"""LocalJudge — 폐쇄망 구성 B/C 의 분류기 (SCRUM-49, F02 LocalJudge 행). Ollama /api/chat + JSON 스키마 강제 출력.

- 입력은 민원 문장뿐 (F02-3). 프롬프트는 ClaudeJudge 와 같은 유형 정의를 쓴다 — 분류 기준이 모델에 따라 달라지면 비교가 안 된다.
- confidence: 로컬 모델의 자기보고 확신도는 보정이 안 되어 있다. LOCAL_JUDGE_SAMPLES>1 이면 temperature 0.7 로 N회 더 뽑아
  **다수 유형의 일치 비율**을 confidence 로 쓴다(반복 샘플링 근사, logprobs 는 Ollama chat 에 없음). 기본 1 (자기보고, 온도 0).
- 실패 시 다른 Judge 로 폴백하지 않는다 (F02-4): 호출 실패 → JudgeError(E-JUDGE), 스키마 2회 위반 → JudgeFormatError(E-LLM-FORMAT).
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter

import httpx
from pydantic import ValidationError

from app.judge.base import DEFAULT_TOOLS, Judgment
from app.judge.claude_judge import CATEGORIES, JudgeError, JudgeFormatError, SYSTEM

# ClaudeJudge 의 SYSTEM 은 "classify 도구" 를 전제로 쓰여 있다 → 로컬은 JSON 으로 답하게 문장만 바꾼다
LOCAL_SYSTEM = SYSTEM.replace("classify 도구로 유형을 하나 고른다", "아래 JSON 형식으로만 답한다") + """

출력 형식 (JSON 한 개, 다른 글자 없이):
{"category": "<유형>", "confidence": <0~1 숫자>, "flags": []}
category 는 TRANSFER_BASIS, MULTI_BROKER, OFFSET, FX_BASIS, TAX_BASIS, FILING, OUT_OF_SCOPE 중 하나. 투자 판단 요구면 flags 에 "ASKS_INVESTMENT_ADVICE"."""

JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": CATEGORIES},
        "confidence": {"type": "number"},
        "flags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["category", "confidence", "flags"],
}


class LocalJudge:
    name = "local"

    def __init__(self, model: str | None = None, host: str | None = None, samples: int | None = None):
        self.model = model or os.getenv("LOCAL_JUDGE_MODEL") or os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
        self.host = (host or os.getenv("OLLAMA_HOST", "http://localhost:11434")).rstrip("/")
        self.samples = samples if samples is not None else int(os.getenv("LOCAL_JUDGE_SAMPLES", "1"))
        self.timeout = float(os.getenv("OLLAMA_TIMEOUT_SEC", "60"))
        self.last_usage = {"calls": 0, "in_tokens": 0, "out_tokens": 0}

    def build_request(self, text: str, temperature: float = 0.0, retry: bool = False) -> dict:
        """API 에 보낼 요청 전체. 고객 정보는 text 뿐 (테스트로 고정)."""
        sys_text = LOCAL_SYSTEM + ("\n\n직전 응답이 형식을 벗어났다. JSON 한 개만, category 는 목록 중 하나로." if retry else "")
        return {
            "model": self.model, "stream": False, "format": JSON_SCHEMA,  # Ollama 구조화 출력 — 스키마에 맞는 JSON 만 생성
            "options": {"temperature": temperature, "num_predict": 120},
            "messages": [{"role": "system", "content": sys_text}, {"role": "user", "content": text}],
        }

    @staticmethod
    def _parse(content: str) -> Judgment | None:
        s = (content or "").strip()
        m = re.search(r"\{.*\}", s, re.S)
        if not m:
            return None
        try:
            d = json.loads(m.group(0))
            d["flags"] = [f for f in (d.get("flags") or []) if f == "ASKS_INVESTMENT_ADVICE"]
            d["confidence"] = min(1.0, max(0.0, float(d.get("confidence", 0))))
            j = Judgment(category=d["category"], confidence=d["confidence"], flags=d["flags"])
        except (ValueError, KeyError, TypeError, ValidationError):
            return None
        j.required_tools = list(DEFAULT_TOOLS[j.category])
        return j

    async def _call(self, req: dict) -> str:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                r = await c.post(f"{self.host}/api/chat", json=req)
                r.raise_for_status()
                data = r.json()
        except Exception as e:  # 연결 실패·타임아웃·5xx. 다른 Judge 로 폴백하지 않는다
            raise JudgeError(f"{type(e).__name__}: {str(e)[:120]}") from e
        self.last_usage["calls"] += 1
        self.last_usage["in_tokens"] += data.get("prompt_eval_count", 0) or 0
        self.last_usage["out_tokens"] += data.get("eval_count", 0) or 0
        return (data.get("message") or {}).get("content") or ""

    async def classify(self, text: str) -> Judgment:
        self.last_usage = {"calls": 0, "in_tokens": 0, "out_tokens": 0}
        j: Judgment | None = None
        for attempt in range(2):
            j = self._parse(await self._call(self.build_request(text, retry=attempt > 0)))
            if j is not None:
                break
        if j is None:
            raise JudgeFormatError("schema violation twice")
        if self.samples > 1:  # 반복 샘플링으로 confidence 근사 — 일치 비율
            votes = Counter([j.category])
            for _ in range(self.samples - 1):
                s = self._parse(await self._call(self.build_request(text, temperature=0.7)))
                if s is not None:
                    votes[s.category] += 1
            top, n = votes.most_common(1)[0]
            if top != j.category:  # 다수결이 온도 0 답과 다르면 다수결을 따른다
                j = Judgment(category=top, confidence=0, flags=j.flags)
                j.required_tools = list(DEFAULT_TOOLS[top])
            j.confidence = round(n / sum(votes.values()), 3)
        return j
