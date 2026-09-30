from __future__ import annotations

import time

from .base import LLM


def _ctx(context: str) -> dict:
    """Only pass `context` when set, so backends without that parameter keep working."""
    return {"context": context} if context else {}


class FallbackLLM(LLM):
    name = 'claude_cli+codex_cli'

    def __init__(self, primary: LLM, fallback: LLM):
        self.primary, self.fallback = primary, fallback
        self.retry_after = 0.0

    async def complete(self, prompt: str, system: str = '', max_tokens: int = 4000, context: str = '') -> str:
        if time.monotonic() >= self.retry_after:
            try:
                return await self.primary.complete(prompt, system, max_tokens, **_ctx(context))
            except Exception as error:
                from .. import db
                db.log(f'Claude unavailable; using Codex plan fallback: {error}', kind='llm', level='warning')
                self.retry_after = time.monotonic() + 900
        return await self.fallback.complete(prompt, system, max_tokens, **_ctx(context))
