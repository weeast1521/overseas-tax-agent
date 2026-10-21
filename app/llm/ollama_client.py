"""Ollama 구현체 (폐쇄망 구간, 구성 B/C). 6일차에 채운다.

Ollama /api/chat 은 tools 파라미터를 지원한다. 모델은 tool calling 되는 것(qwen2.5-coder, llama3.1 등)으로.
"""
from __future__ import annotations

import json
import os
import uuid

import httpx

from app.llm.base import Completion, Message, ToolCall, ToolSpec


class OllamaClient:
    name = "ollama"

    def __init__(self, model: str | None = None, host: str | None = None):
        self.model = model or os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
        self.host = host or os.getenv("OLLAMA_HOST", "http://localhost:11434")

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
            "model": self.model, "stream": False, "options": {"temperature": 0},
            "messages": conv,
            "tools": [{"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.input_schema}} for t in tools],
        }
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.post(f"{self.host}/api/chat", json=body)
            r.raise_for_status()
            data = r.json()
        msg = data["message"]
        call = None
        if msg.get("tool_calls"):
            fc = msg["tool_calls"][0]["function"]
            call = ToolCall(id=str(uuid.uuid4()), name=fc["name"], args=fc.get("arguments") or {})
        return Completion(text=msg.get("content") or None, tool_call=call,
                          in_tokens=data.get("prompt_eval_count", 0), out_tokens=data.get("eval_count", 0))
