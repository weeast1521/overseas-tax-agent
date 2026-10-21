"""F03-8 LLMClient — 에이전트가 보는 유일한 LLM 인터페이스.

구현체: ClaudeClient(개발·구성 A), OllamaClient(폐쇄망·구성 B/C). 환경변수 LLM_PROVIDER로 선택.
"""
from __future__ import annotations

from typing import Any, Literal, Protocol

from pydantic import BaseModel


class Message(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    tool_call_id: str | None = None  # role=tool 일 때
    name: str | None = None


class ToolSpec(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]


class ToolCall(BaseModel):
    id: str
    name: str
    args: dict[str, Any]


class Completion(BaseModel):
    text: str | None = None
    tool_call: ToolCall | None = None
    in_tokens: int = 0
    out_tokens: int = 0

    @property
    def final(self) -> bool:
        return self.tool_call is None and self.text is not None


class LLMClient(Protocol):
    name: str

    async def complete(self, messages: list[Message], tools: list[ToolSpec]) -> Completion: ...


def get_client() -> LLMClient:
    import os
    provider = os.getenv("LLM_PROVIDER", "claude")
    if provider == "claude":
        from app.llm.claude_client import ClaudeClient
        return ClaudeClient()
    if provider == "ollama":
        from app.llm.ollama_client import OllamaClient
        return OllamaClient()
    raise ValueError(f"unknown LLM_PROVIDER={provider}")
