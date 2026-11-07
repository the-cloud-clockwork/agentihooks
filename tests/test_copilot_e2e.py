"""Subprocess-level copilot-target tests: the one-JSON-object contract.

Copilot, like codex, parses hook stdout as EXACTLY ONE JSON object. These
tests drive the real ``python -m hooks`` entry point end-to-end (subprocess,
not a mock) so the contract is verified at the actual boundary Copilot reads,
under ``AGENTIHOOKS_TARGET=copilot``.

They also cover what copilot does that codex cannot: camelCase payloads, its
own event vocabulary, and an allow/ask/deny PreToolUse channel.

Reuses the suite's conftest isolation (``_isolate_real_user_paths`` is
autouse) and the ``AGENTIHOOKS_HOME`` / ``AGENTIHOOKS_DISABLE_BYPASS_LOOKUP``
pattern from ``tests/test_hook_manager.py``.
"""

import json
import os
import subprocess
import sys
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
    """The one-JSON-object contract: nothing, or exactly one parseable object.

    Raises if not parseable — that IS the assertion.
    """
    stripped = stdout.strip()
    if not stripped:
        return
    lines = stripped.splitlines()
    assert len(lines) == 1, f"copilot must receive AT MOST one stdout line, got {len(lines)}: {lines!r}"
    json.loads(lines[0])


def _env(tmp_path, **extra) -> dict:
    return {
        **os.environ,
        "AGENTIHOOKS_TARGET": "copilot",
        "AGENTIHOOKS_HOME": str(tmp_path / "empty_agentihooks"),
        "AGENTIHOOKS_DISABLE_BYPASS_LOOKUP": "1",
        "AGENTIHOOKS_SECRETS_MODE": "standard",
        **extra,
    }


class TestCopilotSessionStartSingleEnvelope:
    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path):
        """A real git repo on main with a clean tree is what makes ensure_on_dev() actually produce a switch message — a
        second buffered source, which is what exercises the envelope contract.
        """
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

    def test_single_json_envelope_on_session_start(self, tmp_path):
        result = _run(
            {"hook_event_name": "SessionStart", "session_id": "copilot-sid-1", "cwd": str(self._repo)},
            _env(tmp_path),
        )
        assert result.returncode == 0, result.stderr
        _assert_stdout_is_empty_or_one_json(result.stdout)

    def test_envelope_context_is_top_level_additional_context(self, tmp_path):
        """Copilot reads TOP-LEVEL additionalContext only — the nested claude
        shape is silently ignored by its parser (proven live, v1.0.80)."""
        result = _run(
            {"hook_event_name": "SessionStart", "session_id": "copilot-sid-1c", "cwd": str(self._repo)},
            _env(tmp_path),
        )
        assert result.returncode == 0, result.stderr
        stripped = result.stdout.strip()
        if stripped:
            envelope = json.loads(stripped)
            assert "additionalContext" in envelope, f"context not top-level: {stripped[:200]}"
            assert "hookSpecificOutput" not in envelope

    def test_camelcase_payload_with_copilot_event_name_dispatches(self, tmp_path):
        """Copilot's own vocabulary must reach the same handler as the alias."""
        result = _run(
            {"hookEventName": "sessionStart", "sessionId": "copilot-sid-1b", "cwd": str(self._repo)},
            _env(tmp_path),
        )
        assert result.returncode == 0, result.stderr
        _assert_stdout_is_empty_or_one_json(result.stdout)


class TestCopilotCiManifestoSingleEnvelope:
    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path):
        manifesto = tmp_path / "MANIFESTO.md"
        manifesto.write_text("# Doctrine\n\nCode is the source of truth.\n")
        self._manifesto_path = str(manifesto)

    def test_single_json_envelope_on_user_prompt_submit(self, tmp_path):
        """Two buffered sources (the secret WARNING and the manifesto payload) is what actually exercises the
        two-envelope bug; with one, an empty buffer makes flush() a no-op and a stray raw print looks like a single
        line by coincidence.
        """
        key = "AKIA" + "TESTDUMMY0000000"
        result = _run(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "copilot-sid-2",
                "prompt": f"my key is {key}",
            },
            _env(
                tmp_path,
                CI_MANIFESTO_RUNTIME_INJECT="true",
                CI_MANIFESTO_PATH=self._manifesto_path,
                CI_MANIFESTO_REFRESH_EVERY="1",
            ),
        )
        assert result.returncode == 0, result.stderr
        _assert_stdout_is_empty_or_one_json(result.stdout)

    def test_copilot_event_name_user_prompt_submitted(self, tmp_path):
        result = _run(
            {
                "hookEventName": "userPromptSubmitted",
                "sessionId": "copilot-sid-2b",
                "prompt": "hello",
            },
            _env(
                tmp_path,
                CI_MANIFESTO_RUNTIME_INJECT="true",
                CI_MANIFESTO_PATH=self._manifesto_path,
                CI_MANIFESTO_REFRESH_EVERY="1",
            ),
        )
        assert result.returncode == 0, result.stderr
        _assert_stdout_is_empty_or_one_json(result.stdout)


class TestCopilotSecretsHardFloor:
    def test_write_with_secret_exits_2(self, tmp_path):
        key = "AKIA" + "TESTDUMMY0000000"
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Write",
            "tool_input": {"file_path": "/tmp/test.py", "content": f"my_key = '{key}'"},
            "session_id": "copilot-sid-3",
            "transcript_path": "",
        }
        result = _run(payload, _env(tmp_path))
        assert result.returncode == 2
        assert result.stderr.strip() != ""
        _assert_stdout_is_empty_or_one_json(result.stdout)

    def test_write_with_secret_blocks_under_copilot_camelcase_payload(self, tmp_path):
        """The HARD FLOOR must not depend on which spelling copilot sends."""
        key = "AKIA" + "TESTDUMMY0000000"
        payload = {
            "hookEventName": "preToolUse",
            "toolName": "Write",
            "toolArgs": {"file_path": "/tmp/test.py", "content": f"my_key = '{key}'"},
            "sessionId": "copilot-sid-3b",
        }
        result = _run(payload, _env(tmp_path))
        assert result.returncode == 2, (
            f"secrets HARD FLOOR did not block a camelCase copilot payload: {result.stdout!r} {result.stderr!r}"
        )
        assert result.stderr.strip() != ""


class TestCopilotRegisteredSpellingsAllBlock:
    """A guardrail must not depend on which spelling copilot echoes back."""

    def test_both_spellings_of_pretooluse_block_a_secret(self, tmp_path):
        key = "AKIA" + "TESTDUMMY0000000"
        for spelling in ("preToolUse", "PreToolUse"):
            result = _run(
                {
                    "hookEventName": spelling,
                    "toolName": "Write",
                    "toolArgs": {"file_path": "/tmp/x.py", "content": f"k = '{key}'"},
                    "sessionId": f"cop-{spelling}",
                },
                _env(tmp_path),
            )
            assert result.returncode == 2, f"{spelling} did not block: {result.stdout!r} {result.stderr!r}"

    def test_deny_is_stated_in_stdout_envelope_as_well_as_exit_2(self, tmp_path):
        """Copilot's runtime calls exit 2 a warning on some events, so the
        denial is also stated in the envelope."""
        key = "AKIA" + "TESTDUMMY0000000"
        result = _run(
            {
                "hookEventName": "preToolUse",
                "toolName": "Write",
                "toolArgs": {"file_path": "/tmp/x.py", "content": f"k = '{key}'"},
                "sessionId": "cop-envelope",
            },
            _env(tmp_path),
        )
        assert result.returncode == 2
        _assert_stdout_is_empty_or_one_json(result.stdout)
        envelope = json.loads(result.stdout.strip())
        assert envelope["decision"] == "block", "copilot parses only the top-level decision shape"
        assert "BLOCKED" in envelope["reason"]

    def test_codex_block_path_emits_no_envelope(self, tmp_path):
        """Unchanged behaviour on codex — exit 2 + stderr only."""
        key = "AKIA" + "TESTDUMMY0000000"
        env = _env(tmp_path)
        env["AGENTIHOOKS_TARGET"] = "codex"
        result = _run(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Write",
                "tool_input": {"file_path": "/tmp/x.py", "content": f"k = '{key}'"},
                "session_id": "codex-envelope",
            },
            env,
        )
        assert result.returncode == 2
        assert result.stdout.strip() == ""


class TestCopilotBlockDrainsBufferedContext:
    def test_inline_secret_note_survives_into_block_stderr(self, tmp_path):
        key = "AKIA" + "TESTDUMMY0000000"
        command = f"export AWS_ACCESS_KEY_ID={key} && git push origin main"
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "session_id": "copilot-sid-4",
            "transcript_path": "",
        }
        result = _run(payload, _env(tmp_path))
        assert result.returncode == 2
        assert "BLOCKED" in result.stderr
        assert "NOTE" in result.stderr, (
            f"buffered inline-secret NOTE did not survive into the block stderr: {result.stderr!r}"
        )
        _assert_stdout_is_empty_or_one_json(result.stdout)


class TestCopilotPreToolUseChannel:
    """Copilot's PreToolUse is a superset of claude's — allow/deny/ask, plus
    context injection. Codex has deny only and no context channel, so this is
    the capability split the capabilities map exists to encode."""

    def test_all_three_permission_decisions_allowed(self):
        from hooks.targets.capabilities import allowed_permission_decisions

        assert allowed_permission_decisions("copilot") == frozenset({"allow", "deny", "ask"})
        assert allowed_permission_decisions("codex") == frozenset({"deny"})

    def test_context_injection_allowed_on_pretooluse(self):
        from hooks.targets.capabilities import can_inject_context

        assert can_inject_context("PreToolUse", "copilot") is True
        assert can_inject_context("PreToolUse", "codex") is False

    def test_arg_mutation_is_copilot_only(self):
        from hooks.targets.capabilities import supports_arg_mutation

        assert supports_arg_mutation("copilot") is True
        assert supports_arg_mutation("codex") is False
        assert supports_arg_mutation("claude") is True


class TestGarbledStdin:
    """Fail-open is the rule; a garbled payload that names a tool-permission
    event gets deny-on-doubt instead — copilot's preToolUse gate is fail-closed
    on non-zero, so exit 0 there reads as "allowed"."""

    def test_garbled_pretooluse_denies(self, tmp_path):
        for raw in (
            'not json {{{ "hookEventName": "preToolUse", "toolName": "Bash"',
            'zzz "hook_event_name": "PreToolUse" zzz',
            'xx "hookEventName": "permissionRequest" {{{',
        ):
            result = subprocess.run(
                [sys.executable, "-m", "hooks"],
                input=raw,
                capture_output=True,
                text=True,
                cwd=_PROJECT_ROOT,
                env=_env(tmp_path),
            )
            assert result.returncode == 2, f"{raw!r} did not deny"
            assert "BLOCKED" in result.stderr

    def test_nested_marker_beyond_head_window_does_not_block(self, tmp_path):
        """The sniff reads only the payload head — a marker EMBEDDED deep in a
        garbled non-tool event must not deny it (a SessionStart carrying a raw
        sample payload in a debug field is not a tool call)."""
        pad = "x" * 600
        raw = (
            '{"hook_event_name": "SessionStart", "session_id": "abc", "pad": "'
            + pad
            + '", "debug": {"hook_event_name": "PreToolUse"'
        )
        result = subprocess.run(
            [sys.executable, "-m", "hooks"],
            input=raw,
            capture_output=True,
            text=True,
            cwd=_PROJECT_ROOT,
            env=_env(tmp_path),
        )
        assert result.returncode == 0, "deep nested marker wrongly denied a SessionStart"

    def test_garbled_non_tool_event_stays_fail_open(self, tmp_path):
        for raw in ("not json at all {{{", 'xx "hook_event_name": "SessionStart" xx{{{', ""):
            result = subprocess.run(
                [sys.executable, "-m", "hooks"],
                input=raw,
                capture_output=True,
                text=True,
                cwd=_PROJECT_ROOT,
                env=_env(tmp_path),
            )
            assert result.returncode == 0, f"{raw!r} wrongly blocked a non-tool event"


class TestRealPayloadContract:
    """Live v1.0.80 stdin is the event's input object alone — no
    hookEventName/hookType field (observed: a dispatched sessionEnd carried
    only reason/sessionId/timestamp/cwd). The wrapper passes the registered
    event as argv[1] and exports AGENTIHOOKS_COPILOT_EVENT; dispatch and the
    HARD FLOOR must work from the env var alone."""

    def test_secret_blocks_with_env_event_and_nameless_payload(self, tmp_path):
        key = "AKIA" + "TESTDUMMY0000000"
        payload = {
            "toolName": "Write",
            "toolArgs": {"file_path": "/tmp/test.py", "content": f"my_key = '{key}'"},
            "sessionId": "copilot-sid-real-1",
            "timestamp": 1787163790624,
            "cwd": "/tmp",
        }
        result = _run(payload, _env(tmp_path, AGENTIHOOKS_COPILOT_EVENT="preToolUse"))
        assert result.returncode == 2, (
            f"nameless real-shaped payload did not block via env event: {result.stdout!r} {result.stderr!r}"
        )
        assert result.stderr.strip() != ""

    def test_nameless_non_tool_payload_without_env_fails_open(self, tmp_path):
        payload = {"reason": "complete", "sessionId": "sid", "timestamp": 1, "cwd": "/tmp"}
        result = _run(payload, _env(tmp_path))
        assert result.returncode == 0

    def test_payload_spelling_wins_over_env(self, tmp_path):
        payload = {"hookEventName": "sessionEnd", "sessionId": "sid", "reason": "complete"}
        result = _run(payload, _env(tmp_path, AGENTIHOOKS_COPILOT_EVENT="preToolUse"))
        assert result.returncode == 0, "a payload-named non-tool event must not inherit the env's deny path"

    def test_garbled_stdin_with_env_pretooluse_denies(self, tmp_path):
        result = subprocess.run(
            [sys.executable, "-m", "hooks"],
            input="not json {{{ no event marker anywhere",
            capture_output=True,
            text=True,
            cwd=_PROJECT_ROOT,
            env=_env(tmp_path, AGENTIHOOKS_COPILOT_EVENT="preToolUse"),
        )
        assert result.returncode == 2, "garbled stdin under an env-named tool event must deny"
        assert "BLOCKED" in result.stderr

    def test_garbled_stdin_with_env_non_tool_event_fails_open(self, tmp_path):
        result = subprocess.run(
            [sys.executable, "-m", "hooks"],
            input="not json {{{",
            capture_output=True,
            text=True,
            cwd=_PROJECT_ROOT,
            env=_env(tmp_path, AGENTIHOOKS_COPILOT_EVENT="sessionEnd"),
        )
        assert result.returncode == 0


class TestToolCallsBatchContract:
    """Live v1.0.80 preToolUse stdin: {"sessionId", "cwd", "toolCalls":
    [{"id", "name", "args": "<json string>"}]} with copilot tool names
    (`create`, `bash`, `edit`, `view`) — no toolName/toolArgs. This shape let
    a secret Write through unscanned before the translation existed."""

    def test_secret_in_toolcalls_create_blocks(self, tmp_path):
        key = "AKIA" + "TESTDUMMY0000000"
        payload = {
            "sessionId": "copilot-batch-1",
            "cwd": "/tmp",
            "toolCalls": [
                {
                    "id": "toolu_x1",
                    "name": "create",
                    "args": json.dumps({"path": "/tmp/creds.py", "file_text": f"aws_key = '{key}'\n"}),
                }
            ],
        }
        result = _run(payload, _env(tmp_path, AGENTIHOOKS_COPILOT_EVENT="preToolUse"))
        assert result.returncode == 2, (
            f"real-shaped toolCalls create with a secret was not denied: {result.stdout!r} {result.stderr!r}"
        )
        assert "BLOCKED" in result.stderr

    def test_secret_in_second_batched_call_blocks(self, tmp_path):
        key = "AKIA" + "TESTDUMMY0000000"
        payload = {
            "sessionId": "copilot-batch-2",
            "cwd": "/tmp",
            "toolCalls": [
                {"id": "a", "name": "view", "args": json.dumps({"path": "/tmp/ok.txt"})},
                {
                    "id": "b",
                    "name": "create",
                    "args": json.dumps({"path": "/tmp/creds.py", "file_text": f"k = '{key}'"}),
                },
            ],
        }
        result = _run(payload, _env(tmp_path, AGENTIHOOKS_COPILOT_EVENT="preToolUse"))
        assert result.returncode == 2, "a secret in the SECOND batched call must deny the batch"

    def test_unparseable_args_still_reach_the_scanner(self, tmp_path):
        key = "AKIA" + "TESTDUMMY0000000"
        payload = {
            "sessionId": "copilot-batch-3",
            "cwd": "/tmp",
            "toolCalls": [{"id": "c", "name": "create", "args": f"not-json k='{key}'"}],
        }
        result = _run(payload, _env(tmp_path, AGENTIHOOKS_COPILOT_EVENT="preToolUse"))
        assert result.returncode == 2, "parse failure must not hide content from the HARD FLOOR"

    def test_bash_name_maps_to_bash_guardrails(self, tmp_path):
        payload = {
            "sessionId": "copilot-batch-4",
            "cwd": "/tmp",
            "toolCalls": [
                {
                    "id": "d",
                    "name": "bash",
                    "args": json.dumps({"command": "git push origin " + "main"}),
                }
            ],
        }
        result = _run(payload, _env(tmp_path, AGENTIHOOKS_COPILOT_EVENT="preToolUse"))
        assert result.returncode == 2, "copilot `bash` tool must hit the Bash branch guards"


class TestBuiltinWriteToolNames:
    """apply_patch and str_replace_editor are Rust-backed builtin WRITE tools
    in the v1.0.80 binary (copilot's own write set:
    new Set(["apply_patch","create","edit","str_replace"])). Unmapped, their
    tool_name stayed foreign and skipped the Write/Edit secrets branch — a
    confirmed HARD FLOOR bypass on a non-default model backend."""

    def _pre(self, tmp_path, name, args):
        return _run(
            {"sessionId": "b", "cwd": "/tmp", "toolCalls": [{"id": "c1", "name": name, "args": json.dumps(args)}]},
            _env(tmp_path, AGENTIHOOKS_COPILOT_EVENT="preToolUse"),
        )

    def test_apply_patch_secret_in_patch_body_blocks(self, tmp_path):
        key = "AKIA" + "TESTDUMMY0000000"
        r = self._pre(tmp_path, "apply_patch", {"patch": f"@@ creds.py @@\n+AWS = {key}\n"})
        assert r.returncode == 2, f"apply_patch write of a secret was not denied: {r.stdout!r} {r.stderr!r}"

    def test_str_replace_editor_str_replace_secret_blocks(self, tmp_path):
        key = "AKIA" + "TESTDUMMY0000000"
        r = self._pre(
            tmp_path,
            "str_replace_editor",
            {"command": "str_replace", "path": "/tmp/creds.py", "old_str": "OLD", "new_str": f"AWS = {key}"},
        )
        assert r.returncode == 2, "str_replace_editor str_replace of a secret was not denied"

    def test_str_replace_editor_create_secret_blocks(self, tmp_path):
        key = "AKIA" + "TESTDUMMY0000000"
        r = self._pre(
            tmp_path,
            "str_replace_editor",
            {"command": "create", "path": "/tmp/creds.py", "file_text": f"AWS = {key}\n"},
        )
        assert r.returncode == 2, "str_replace_editor create of a secret was not denied"


class TestEnvEventGatedOnTarget:
    """AGENTIHOOKS_COPILOT_EVENT drives the garbled-stdin deny-on-doubt sniff,
    but the var can be inherited by a nested claude/codex hook spawned from
    within a copilot hook. Trusting it off-target would deny an unrelated
    garbled-stdin claude/codex event that names no tool at all."""

    def _garbled(self, tmp_path, target, **extra):
        env = {
            **os.environ,
            "AGENTIHOOKS_HOME": str(tmp_path / "ah"),
            "AGENTIHOOKS_DISABLE_BYPASS_LOOKUP": "1",
            "AGENTIHOOKS_SECRETS_MODE": "standard",
            "AGENTIHOOKS_COPILOT_EVENT": "permissionRequest",
            **extra,
        }
        if target is None:
            env.pop("AGENTIHOOKS_TARGET", None)
        else:
            env["AGENTIHOOKS_TARGET"] = target
        return subprocess.run(
            [sys.executable, "-m", "hooks"],
            input="{garbled no event marker",
            capture_output=True,
            text=True,
            cwd=_PROJECT_ROOT,
            env=env,
        )

    def test_inherited_env_does_not_deny_a_claude_garbled_event(self, tmp_path):
        r = self._garbled(tmp_path, None)
        assert r.returncode == 0, f"claude garbled stdin wrongly denied via inherited copilot env: {r.stderr!r}"

    def test_inherited_env_does_not_deny_a_codex_garbled_event(self, tmp_path):
        r = self._garbled(tmp_path, "codex")
        assert r.returncode == 0, f"codex garbled stdin wrongly denied via inherited copilot env: {r.stderr!r}"

    def test_copilot_still_denies_on_its_env_event(self, tmp_path):
        r = self._garbled(tmp_path, "copilot")
        assert r.returncode == 2, "copilot must still deny a garbled tool-permission event named by its env var"
