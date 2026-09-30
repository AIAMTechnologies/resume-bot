from __future__ import annotations

from abc import ABC, abstractmethod


class LLMError(RuntimeError):
    pass


class LLM(ABC):
    name: str = "base"

    @abstractmethod
    async def complete(self, prompt: str, system: str = "", max_tokens: int = 4000, context: str = "") -> str:
        """Single-turn completion. Returns the text reply.

        `context` is reused material (e.g. your profile) sent before `prompt`. Keep it identical
        across calls so backends that support prompt caching can bill it at the cached rate.
        """
