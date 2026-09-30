"""Anthropic Messages API backend."""
from __future__ import annotations

import anthropic

from ..config import env
from .base import LLM, LLMError


class AnthropicAPI(LLM):
    name = "anthropic_api"

    def __init__(self):
        if not env().anthropic_api_key:
            raise LLMError("ANTHROPIC_API_KEY is not set")
        self.client = anthropic.AsyncAnthropic(api_key=env().anthropic_api_key)
        self.model = env().llm_model

    async def complete(self, prompt: str, system: str = "", max_tokens: int = 4000) -> str:
        try:
            msg = await self.client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system or anthropic.NOT_GIVEN,
                messages=[{"role": "user", "content": prompt}],
            )
        except anthropic.APIError as e:
            raise LLMError(str(e)) from e
        return "".join(b.text for b in msg.content if b.type == "text")
