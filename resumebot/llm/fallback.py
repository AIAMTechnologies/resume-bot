"""Two-way plan failover: use whichever AI plan has allowance left, in either direction.

Backends are tried in your configured order. When one runs out (session limit, allowance
exhausted, overloaded…), it rests for a cool-down and the other plan takes over. If the plan
you're on runs out while the other is still cooling down, the other is tried anyway — its
allowance may have reset — so work only stops when every plan is genuinely unavailable.
"""
from __future__ import annotations

import time

from .base import LLM


def _ctx(context: str) -> dict:
    """Only pass `context` when set, so backends without that parameter keep working."""
    return {"context": context} if context else {}


class FallbackLLM(LLM):
    def __init__(self, *backends: LLM, cooldown: float = 900):
        if len(backends) < 2:
            raise ValueError("FallbackLLM needs at least two backends")
        self.backends = list(backends)
        self.cooldown = cooldown
        self.down_until = [0.0] * len(self.backends)
        self.name = "+".join(getattr(b, "name", type(b).__name__) for b in self.backends)

    def __getattr__(self, name: str):
        """Settings like .model / .client read from the primary plan."""
        if name in ("backends", "cooldown", "down_until"):
            raise AttributeError(name)
        return getattr(self.backends[0], name)

    def _order(self) -> list[int]:
        """Available plans first (in configured order), then cooling-down plans, soonest reset first."""
        now = time.monotonic()
        ready = [i for i in range(len(self.backends)) if self.down_until[i] <= now]
        resting = sorted((i for i in range(len(self.backends)) if self.down_until[i] > now),
                         key=lambda i: self.down_until[i])
        return ready + resting

    async def complete(self, prompt: str, system: str = "", max_tokens: int = 4000, context: str = "") -> str:
        last_error: Exception | None = None
        for position, i in enumerate(self._order()):
            backend = self.backends[i]
            try:
                reply = await backend.complete(prompt, system, max_tokens, **_ctx(context))
                self.down_until[i] = 0.0
                return reply
            except Exception as error:  # noqa: BLE001 — any failure: rest this plan, try the other
                last_error = error
                self.down_until[i] = time.monotonic() + self.cooldown
                nxt = self._order()
                other = next((self.backends[j] for j in nxt if j != i), None)
                if other is not None and position < len(self.backends) - 1:
                    from .. import db
                    db.log(f"{getattr(backend, 'name', 'AI plan')} unavailable; switching to "
                           f"{getattr(other, 'name', 'the other plan')}: {error}"[:300], kind="llm", level="warning")
        assert last_error is not None
        raise last_error
