"""Subprocess-level codex-target tests: the one-JSON-object contract.

Codex parses hook stdout as EXACTLY ONE JSON object (unlike Claude Code,
which concatenates every raw stdout line). Several handlers used to print
their own ``hookSpecificOutput`` envelope directly instead of routing
through ``hooks.targets.emitter`` — on codex that produced a second stdout
line and broke the host's JSON parse. These tests drive the real ``python -m
hooks`` entry point end-to-end (subprocess, not a mock) so the contract is
verified at the actual boundary codex reads.

Reuses the suite's conftest isolation (``_isolate_real_user_paths`` is
autouse) and the ``AGENTIHOOKS_HOME`` / ``AGENTIHOOKS_DISABLE_BYPASS_LOOKUP``
pattern from ``tests/test_hook_manager.py`` rather than a second fixture.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_PROJECT_ROOT = Path(__file__).parent.parent


def _run(payload: dict, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "hooks"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=_PROJECT_ROOT,
        env=env,
    )


def _assert_stdout_is_empty_or_one_json(stdout: str) -> None:
    """The one-JSON-object contract: nothing, or exactly one parseable object."""
    stripped = stdout.strip()
    if not stripped:
        return
    lines = stripped.splitlines()
    assert len(lines) == 1, f"codex must receive AT MOST one stdout line, got {len(lines)}: {lines!r}"
    json.loads(lines[0])  # raises if not parseable — that IS the assertion


class TestCodexSessionStartSingleEnvelope:
    """auto_dev_switch used to print its own envelope, bypassing the emitter
    buffer — on codex that landed as a second stdout line alongside the
    flush() envelope. Reproduced pre-fix: 2 lines, json.loads raises
    'Extra data'."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path):
        self._empty_home = str(tmp_path / "empty_agentihooks")
        (tmp_path / "empty_agentihooks").mkdir()
        (tmp_path / "empty_agentihooks" / "deps.stamp").write_text(json.dumps({"ok_at": time.time()}))
        # A real git repo on main/master with a clean tree is what makes
        # ensure_on_dev() actually produce a switch message — the exact
        # condition that fed the second print.
        repo = tmp_path / "repo"
        repo.mkdir()
        run = lambda *args: subprocess.run(  # noqa: E731
            ["git", *args], cwd=repo, capture_output=True, text=True, check=True
        )
        run("init", "-b", "main")
        run("config", "user.email", "test@example.com")
        run("config", "user.name", "Test")
        (repo / "README.md").write_text("hello\n")
        run("add", "README.md")
        run("commit", "-m", "initial")
        self._repo = repo

    def test_single_json_envelope_on_session_start(self):
        env = {
            **os.environ,
            "AGENTIHOOKS_TARGET": "codex",
            "AGENTIHOOKS_HOME": self._empty_home,
            "AGENTIHOOKS_DISABLE_BYPASS_LOOKUP": "1",
            "AGENTIHOOKS_FORCE_DEV_BRANCH": "true",
        }
        result = _run(
            {"hook_event_name": "SessionStart", "session_id": "codex-sid-1", "cwd": str(self._repo)},
            env,
        )
        assert result.returncode == 0, result.stderr
        _assert_stdout_is_empty_or_one_json(result.stdout)


class TestCodexCiManifestoSingleEnvelope:
    """ci_manifesto.inject_on_session_start / maybe_refresh had the same
    direct-print bug, gated behind CI_MANIFESTO_RUNTIME_INJECT."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path):
        self._empty_home = str(tmp_path / "empty_agentihooks")
        manifesto = tmp_path / "MANIFESTO.md"
        manifesto.write_text("# Doctrine\n\nCode is the source of truth.\n")
        self._manifesto_path = str(manifesto)

    def _env(self, **extra) -> dict:
        return {
            **os.environ,
            "AGENTIHOOKS_TARGET": "codex",
            "AGENTIHOOKS_HOME": self._empty_home,
            "AGENTIHOOKS_DISABLE_BYPASS_LOOKUP": "1",
            "AGENTIHOOKS_SECRETS_MODE": "standard",
            "CI_MANIFESTO_RUNTIME_INJECT": "true",
            "CI_MANIFESTO_PATH": self._manifesto_path,
            **extra,
        }

    def test_single_json_envelope_on_user_prompt_submit(self):
        # A prompt-secret hit also buffers a WARNING via inject_context() —
        # that call site already routed through the emitter correctly before
        # this fix. Without a second buffered source, an empty buffer makes
        # flush() a no-op and the pre-fix bug (ci_manifesto's own raw print)
        # would look like exactly one line by coincidence. Forcing a second
        # buffered entry is what actually exercises the two-envelope bug:
        # pre-fix this is [flushed envelope, ci_manifesto's raw print] = 2
        # lines; post-fix both fold into the one flushed envelope.
        key = "AKIA" + "TESTDUMMY0000000"
        env = self._env(CI_MANIFESTO_REFRESH_EVERY="1")
        result = _run(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "codex-sid-2",
                "prompt": f"my key is {key}",
            },
            env,
        )
        assert result.returncode == 0, result.stderr
        _assert_stdout_is_empty_or_one_json(result.stdout)


class TestCodexSecretsHardFloor:
    """The secrets-in-file HARD FLOOR has no automated codex coverage before
    this — verify the deny-only PreToolUse path actually exits 2 there."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path):
        self._empty_home = str(tmp_path / "empty_agentihooks")

    def _env(self) -> dict:
        return {
            **os.environ,
            "AGENTIHOOKS_TARGET": "codex",
            "AGENTIHOOKS_HOME": self._empty_home,
            "AGENTIHOOKS_DISABLE_BYPASS_LOOKUP": "1",
            "AGENTIHOOKS_SECRETS_MODE": "standard",
        }

    def test_write_with_secret_exits_2(self):
        key = "AKIA" + "TESTDUMMY0000000"
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Write",
            "tool_input": {"file_path": "/tmp/test.py", "content": f"my_key = '{key}'"},
            "session_id": "codex-sid-3",
            "transcript_path": "",
        }
        result = _run(payload, self._env())
        assert result.returncode == 2
        assert result.stderr.strip() != ""
        _assert_stdout_is_empty_or_one_json(result.stdout)


class TestCodexBlockDrainsBufferedContext:
    """A BlockAction used to skip the flush entirely, so on codex any context
    a handler buffered before the block (e.g. the inline-secret NOTE) was
    silently lost — stdout carries nothing on the block path. The fix folds
    the drained buffer into the stderr message after the block reason."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path):
        self._empty_home = str(tmp_path / "empty_agentihooks")

    def _env(self) -> dict:
        return {
            **os.environ,
            "AGENTIHOOKS_TARGET": "codex",
            "AGENTIHOOKS_HOME": self._empty_home,
            "AGENTIHOOKS_DISABLE_BYPASS_LOOKUP": "1",
            "AGENTIHOOKS_SECRETS_MODE": "standard",
        }

    def test_inline_secret_note_survives_into_block_stderr(self):
        key = "AKIA" + "TESTDUMMY0000000"
        command = f"export AWS_ACCESS_KEY_ID={key} && git push origin main"
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "session_id": "codex-sid-4",
            "transcript_path": "",
        }
        result = _run(payload, self._env())
        assert result.returncode == 2
        assert "BLOCKED" in result.stderr
        assert "NOTE" in result.stderr, (
            f"buffered inline-secret NOTE did not survive into the block stderr: {result.stderr!r}"
        )
        _assert_stdout_is_empty_or_one_json(result.stdout)
