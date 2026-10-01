import pytest

from resumebot.llm.base import LLMError
from resumebot.llm.codex_cli import check_allowance
from resumebot.llm.fallback import FallbackLLM


def test_plan_only_preflight():
    limit = {'credits': {'hasCredits': False, 'unlimited': False}, 'primary': {'usedPercent': 30}}
    check_allowance({'rateLimits': limit})
    for changed in [{}, {**limit, 'credits': {'hasCredits': True}},
                    {**limit, 'primary': {'usedPercent': 99}}, {**limit, 'spendControlReached': True}]:
        with pytest.raises(LLMError):
            check_allowance({'rateLimits': changed})


async def test_fallback_cools_down_primary():
    class Primary:
        calls = 0
        async def complete(self, *args):
            self.calls += 1
            raise LLMError('session limit')
    class Backup:
        async def complete(self, *args):
            return 'backup response'
    primary = Primary()
    llm = FallbackLLM(primary, Backup())
    assert await llm.complete('one') == 'backup response'
    assert await llm.complete('two') == 'backup response'
    assert primary.calls == 1


async def test_codex_uses_chatgpt_and_removes_api_credentials(monkeypatch):
    from pathlib import Path
    from resumebot.llm import codex_cli
    monkeypatch.setenv('OPENAI_API_KEY', 'test-must-not-leak')
    monkeypatch.setenv('CODEX_API_KEY', 'test-must-not-leak')
    backend = codex_cli.CodexCLI()
    backend.path = 'codex'
    async def allowance():
        pass
    monkeypatch.setattr(backend, '_allowance', allowance)
    async def spawn(*args, **kwargs):
        assert 'forced_login_method="chatgpt"' in args
        assert '--ignore-user-config' in args
        assert '--ephemeral' in args
        assert 'OPENAI_API_KEY' not in kwargs['env']
        assert 'CODEX_API_KEY' not in kwargs['env']
        class Process:
            returncode = 0
            async def communicate(self, payload):
                assert b'Resume input' in payload
                Path(args[args.index('-o') + 1]).write_text('Resume output')
                return b'', b''
        return Process()
    monkeypatch.setattr(codex_cli.asyncio, 'create_subprocess_exec', spawn)
    assert await backend.complete('Resume input') == 'Resume output'


async def test_claude_error_extracts_useful_limit_message(monkeypatch):
    from resumebot.llm import claude_cli
    backend = claude_cli.ClaudeCLI()
    backend.path = 'claude'
    async def spawn(*args, **kwargs):
        class Process:
            returncode = 1
            async def communicate(self, payload):
                return b'{"duration_api_ms":0,"is_error":true,"result":"Session limit reached"}', b''
        return Process()
    monkeypatch.setattr(claude_cli.asyncio, 'create_subprocess_exec', spawn)
    with pytest.raises(LLMError, match='Session limit reached'):
        await backend.complete('hello')



async def test_switches_both_ways_when_each_plan_runs_out():
    class Plan:
        def __init__(self, name):
            self.name, self.out, self.calls = name, False, 0
        async def complete(self, *args, **kwargs):
            self.calls += 1
            if self.out:
                raise LLMError(f"{self.name} allowance exhausted")
            return self.name
    claude, codex = Plan("claude"), Plan("codex")
    llm = FallbackLLM(claude, codex)
    assert await llm.complete("a") == "claude"
    claude.out = True
    assert await llm.complete("b") == "codex"          # Claude out -> Codex
    codex.out, claude.out = True, False               # Codex runs out, Claude has reset
    assert await llm.complete("c") == "claude"         # Codex out -> back to Claude, despite its cool-down
    codex.out = False
    claude.out = True
    assert await llm.complete("d") == "codex"          # and over again
    claude.out = codex.out = True
    with pytest.raises(LLMError):
        await llm.complete("e")                        # only fails when both are out


def test_either_plan_can_be_primary(monkeypatch):
    from types import SimpleNamespace
    from resumebot import llm
    from resumebot.llm.claude_cli import ClaudeCLI
    from resumebot.llm.codex_cli import CodexCLI
    monkeypatch.setattr(llm, "env", lambda: SimpleNamespace(llm_backend="codex_cli", llm_fallback="claude_cli",
                                                            llm_model="m", llm_fast_model=""))
    llm._llm.cache_clear()
    try:
        chain = llm.get_llm()
        assert isinstance(chain.backends[0], CodexCLI) and isinstance(chain.backends[1], ClaudeCLI)
    finally:
        llm._llm.cache_clear()
