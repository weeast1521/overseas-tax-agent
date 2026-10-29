"""Ollama 구현체 (폐쇄망 구간, 구성 B/C) — SCRUM-49.

- /api/chat 의 tools 파라미터로 툴 호출. 모델은 tool calling 되는 것(qwen2.5, llama3.1, exaone3.5 등).
- 로컬 모델은 툴 호출 형식을 자주 어긴다 → tool_calls 가 없으면 본문에서 JSON 블록(`{"name": …, "arguments": …}` 또는
  `{"tool": …, "args": …}`)을 찾아 ToolCall 로 복원한다. 같은 프로세스 안의 파싱이지 다른 모델로의 폴백이 아니다.
- 온도 0, num_ctx 는 OLLAMA_NUM_CTX(기본 16384) — Ollama 기본 2048 이면 에이전트 문맥(8\~10k 토큰)이 잘려 조용히 망가진다.
- 실패는 예외로 올린다 → react 루프가 E-LLM 으로 기록 (폴백 없음, SCRUM-51).
"""
from __future__ import annotations

import json
import os
import re
import uuid

import httpx

from app.llm.base import Completion, Message, ToolCall, ToolSpec


class OllamaClient:
    name = "ollama"

    def __init__(self, model: str | None = None, host: str | None = None):
        self.model = model or os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
        self.host = (host or os.getenv("OLLAMA_HOST", "http://localhost:11434")).rstrip("/")
        self.num_ctx = int(os.getenv("OLLAMA_NUM_CTX", "16384"))
        self.timeout = float(os.getenv("OLLAMA_TIMEOUT_SEC", "120"))
        self.last_usage = {"calls": 0, "in_tokens": 0, "out_tokens": 0, "parsed_from_text": 0}

    async def complete(self, messages: list[Message], tools: list[ToolSpec]) -> Completion:
        conv = []
        for m in messages:
            if m.role == "tool":
                conv.append({"role": "tool", "content": m.content})
            elif m.role == "assistant" and m.name:
                conv.append({"role": "assistant", "content": "", "tool_calls": [{"function": {"name": m.name, "arguments": json.loads(m.content)}}]})
            else:
                conv.append({"role": m.role, "content": m.content})
        body = {
            "model": self.model, "stream": False, "options": {"temperature": 0, "num_ctx": self.num_ctx},
            "messages": conv,
            "tools": [{"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.input_schema}} for t in tools],
        }
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r = await c.post(f"{self.host}/api/chat", json=body)
            r.raise_for_status()
            data = r.json()
        msg = data["message"]
        text = msg.get("content") or None
        call = None
        if msg.get("tool_calls"):
            fc = msg["tool_calls"][0]["function"]
            args = fc.get("arguments") or {}
            if isinstance(args, str):  # 일부 모델은 arguments 를 문자열 JSON 으로 준다
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            call = ToolCall(id=str(uuid.uuid4()), name=fc["name"], args=args)
        elif text and tools:
            call = parse_tool_call_from_text(text, {t.name for t in tools})
            if call is not None:
                self.last_usage["parsed_from_text"] += 1
                text = None
        self.last_usage["calls"] += 1
        self.last_usage["in_tokens"] += data.get("prompt_eval_count", 0) or 0
        self.last_usage["out_tokens"] += data.get("eval_count", 0) or 0
        return Completion(text=text, tool_call=call,
                          in_tokens=data.get("prompt_eval_count", 0) or 0, out_tokens=data.get("eval_count", 0) or 0)


_FENCE = re.compile(r"```(?:json|tool_call|tool)?\s*(\{.*?\})\s*```", re.S)
_TAG = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)


def parse_tool_call_from_text(text: str, allowed: set[str]) -> ToolCall | None:
    """본문에 적힌 툴 호출 JSON 을 복원한다. 허용 툴 이름이 아니면 None (모델이 지어낸 툴은 E-TOOL-UNKNOWN 이 아니라 그냥 텍스트로 둔다)."""
    cands = _TAG.findall(text) + _FENCE.findall(text)
    if not cands:
        m = re.search(r"\{[^{}]*\"(?:name|tool)\"[^{}]*(?:\{[^{}]*\}[^{}]*)?\}", text, re.S)
        if m:
            cands = [m.group(0)]
    for c in cands:
        try:
            d = json.loads(c)
        except ValueError:
            continue
        if not isinstance(d, dict):
            continue
        name = d.get("name") or d.get("tool") or (d.get("function") or {}).get("name")
        args = d.get("arguments") if "arguments" in d else d.get("args", d.get("parameters", d.get("input")))
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {}
        if name in allowed:
            return ToolCall(id=str(uuid.uuid4()), name=name, args=args if isinstance(args, dict) else {})
    return None
