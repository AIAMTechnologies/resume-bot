"""Claude Code CLI backend (`claude -p`). Uses the Claude plan you're logged in with."""
from __future__ import annotations

import asyncio
import glob
import json
import os
import shutil
import tempfile
from pathlib import Path

from ..config import env
from .base import LLM, LLMError

# Headless runs are single-turn text transforms: no tools, no project context, no saved sessions.
_BASE_ARGS = ["-p", "--output-format", "json", "--tools", "", "--no-session-persistence",
              "--setting-sources", ""]


def find_claude_cli() -> str | None:
    configured = env().claude_cli_path
    if configured and Path(configured).exists():
        return configured
    found = shutil.which("claude")
    if found:
        return found
    home = Path.home()
    for candidate in (home / ".local/bin/claude", home / ".claude/local/claude"):
        if candidate.exists():
            return str(candidate)
    # Claude desktop app bundles the CLI; pick the newest version.
    bundled = sorted(
        glob.glob(str(home / "Library/Application Support/Claude/claude-code/*/claude.app/Contents/MacOS/claude")),
        key=lambda p: [int(x) if x.isdigit() else x for x in Path(p).parts[-5].split(".")],
    )
    return bundled[-1] if bundled else None


class ClaudeCLI(LLM):
    name = "claude_cli"

    def __init__(self, concurrency: int = 2, timeout: float = 300):
        self.path = find_claude_cli()
        self.model = env().llm_model
        self.timeout = timeout
        self._sem = asyncio.Semaphore(concurrency)
        self._cwd = tempfile.mkdtemp(prefix="resumebot-llm-")
        self._env = {**os.environ, "CLAUDE_CODE_ENTRYPOINT": "resumebot"}
        if env().claude_code_oauth_token:
            self._env["CLAUDE_CODE_OAUTH_TOKEN"] = env().claude_code_oauth_token

    async def complete(self, prompt: str, system: str = "", max_tokens: int = 4000) -> str:
        if not self.path:
            raise LLMError("Claude Code CLI not found. Install it or set CLAUDE_CLI_PATH in .env.")
        args = [self.path, *_BASE_ARGS, "--model", self.model]
        if system:
            args += ["--system-prompt", system]
        async with self._sem:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._cwd,
                env=self._env,
            )
            try:
                out, err = await asyncio.wait_for(proc.communicate(prompt.encode()), self.timeout)
            except asyncio.TimeoutError:
                proc.kill()
                raise LLMError("claude CLI timed out")
        if proc.returncode != 0:
            raise LLMError(f"claude CLI failed ({proc.returncode}): {err.decode()[:500] or out.decode()[:500]}")
        try:
            payload = json.loads(out.decode())
        except json.JSONDecodeError:
            return out.decode()
        if payload.get("is_error"):
            raise LLMError(f"claude CLI error: {payload.get('result') or payload}")
        return payload.get("result", "")
