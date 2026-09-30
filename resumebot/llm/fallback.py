from __future__ import annotations

import time

from .base import LLM


class FallbackLLM(LLM):
    name = 'claude_cli+codex_cli'

    def __init__(self, primary: LLM, fallback: LLM):
        self.primary, self.fallback = primary, fallback
        self.retry_after = 0.0

    async def complete(self, prompt: str, system: str = '', max_tokens: int = 4000) -> str:
        if time.monotonic() >= self.retry_after:
            try:
                return await self.primary.complete(prompt, system, max_tokens)
            except Exception as error:
                from .. import db
                db.log(f'Claude unavailable; using Codex plan fallback: {error}', kind='llm', level='warning')
                self.retry_after = time.monotonic() + 900
        return await self.fallback.complete(prompt, system, max_tokens)
