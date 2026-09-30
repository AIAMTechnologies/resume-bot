"""Anthropic Messages API backend."""
from __future__ import annotations

import anthropic

from ..config import env
from .base import LLM, LLMError


class AnthropicAPI(LLM):
    name = "anthropic_api"

    def __init__(self, model: str = ""):
        if not env().anthropic_api_key:
            raise LLMError("ANTHROPIC_API_KEY is not set")
        self.client = anthropic.AsyncAnthropic(api_key=env().anthropic_api_key)
        self.model = model or env().llm_model

    async def complete(self, prompt: str, system: str = "", max_tokens: int = 4000, context: str = "") -> str:
        content: list[dict] = []
        if context:
            # Cache breakpoint after the stable part (system + context); only the prompt varies.
            content.append({"type": "text", "text": context, "cache_control": {"type": "ephemeral"}})
        content.append({"type": "text", "text": prompt})
        try:
            msg = await self.client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system or anthropic.NOT_GIVEN,
                messages=[{"role": "user", "content": content}],
            )
        except anthropic.APIError as e:
            raise LLMError(str(e)) from e
        return "".join(b.text for b in msg.content if b.type == "text")
