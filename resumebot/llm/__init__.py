"""LLM access. `get_llm()` returns the configured backend.

- claude_cli: shells out to `claude -p` (Claude Code), billed against your Claude plan.
- anthropic_api: the Anthropic Messages API (ANTHROPIC_API_KEY).
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Any

from ..config import env
from .base import LLM, LLMError


@lru_cache
def get_llm() -> LLM:
    backend = env().llm_backend
    if backend == "codex_cli":
        from .codex_cli import CodexCLI
        return CodexCLI()
    if backend == "anthropic_api":
        from .anthropic_api import AnthropicAPI
        return AnthropicAPI()
    from .claude_cli import ClaudeCLI
    primary = ClaudeCLI()
    if env().llm_fallback == "codex_cli":
        from .codex_cli import CodexCLI
        from .fallback import FallbackLLM
        return FallbackLLM(primary, CodexCLI())
    return primary


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def parse_json(text: str) -> Any:
    """Pull the first JSON object/array out of a model reply."""
    m = _FENCE.search(text)
    if m:
        text = m.group(1)
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if not starts:
        raise LLMError(f"No JSON in model reply: {text[:200]}")
    start = min(starts)
    closer = "}" if text[start] == "{" else "]"
    end = text.rfind(closer)
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError as e:
        raise LLMError(f"Bad JSON in model reply: {e}: {text[:200]}") from e


async def complete_json(prompt: str, system: str = "", max_tokens: int = 4000) -> Any:
    llm = get_llm()
    reply = await llm.complete(prompt + "\n\nRespond with JSON only, no prose.", system=system,
                               max_tokens=max_tokens)
    return parse_json(reply)


__all__ = ["get_llm", "parse_json", "complete_json", "LLM", "LLMError"]
