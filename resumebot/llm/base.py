from __future__ import annotations

from abc import ABC, abstractmethod


class LLMError(RuntimeError):
    pass


class LLM(ABC):
    name: str = "base"

    @abstractmethod
    async def complete(self, prompt: str, system: str = "", max_tokens: int = 4000) -> str:
        """Single-turn completion. Returns the text reply."""
