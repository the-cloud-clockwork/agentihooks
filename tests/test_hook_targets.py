"""Tests for the hook-runtime target layer (hooks/targets/).

Covers target detection, the Codex and Copilot payload normalizers, the
per-(target, event) capability map, and the buffer-then-flush emitter contract
(both parse hook stdout as ONE JSON object).
"""

import json

import pytest

from hooks.targets import (
    buffers_single_envelope,
    current_target,
    global_record,
    is_codex,
    is_copilot,
    split_global,
)
from hooks.targets.capabilities import (
    allowed_permission_decisions,
    can_inject_context,
    requires_envelope_block,
    supports_arg_mutation,
)
from hooks.targets.emitter import buffer_context, drain, flush, has_buffered
from hooks.targets.normalizer import normalize_payload


@pytest.fixture
def codex(monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_TARGET", "codex")


@pytest.fixture
def copilot(monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_TARGET", "copilot")


@pytest.fixture
def claude(monkeypatch):
    monkeypatch.delenv("AGENTIHOOKS_TARGET", raising=False)


class TestDetection:
    def test_default_is_claude(self, claude):
        assert current_target() == "claude"
        assert not is_codex()

    def test_env_marker_selects_codex(self, codex):
        assert current_target() == "codex"
        assert is_codex()
        assert not is_copilot()

    def test_env_marker_selects_copilot(self, copilot):
        assert current_target() == "copilot"
        assert is_copilot()
        assert not is_codex()

    def test_envelope_buffering_is_target_capability_not_codex_identity(self, claude, codex, copilot):
        # Fixtures apply in order; the last one wins. Assert against explicit
        # arguments so this does not depend on that ordering.
        assert buffers_single_envelope("codex") is True
        assert buffers_single_envelope("copilot") is True
        assert buffers_single_envelope("claude") is False


class TestNormalizer:
    def test_claude_payload_untouched(self, claude):
        payload = {"hook_event_name": "PostToolUse", "tool_response": {"out": 1}}
        assert normalize_payload(dict(payload)) == payload

    def test_codex_fills_output_aliases(self, codex):
        payload = normalize_payload(
            {"hook_event_name": "PostToolUse", "tool_name": "shell", "tool_response": {"out": 1}}
        )
        assert payload["tool_output"] == {"out": 1}
        assert payload["tool_result"] == {"out": 1}

    def test_codex_does_not_clobber_existing_aliases(self, codex):
        payload = normalize_payload({"tool_response": "new", "tool_output": "old"})
        assert payload["tool_output"] == "old"


class TestContextCaps:
    def test_copilot_post_tool_use_is_capped(self, copilot, capsys):
        from hooks.targets import emitter

        emitter.buffer_context("x" * 20000)
        emitter.flush("PostToolUse")
        payload = json.loads(capsys.readouterr().out)
        assert len(payload["additionalContext"].encode()) == 10 * 1024
        assert payload["additionalContext"].endswith("limit]")

    def test_uncapped_events_pass_through(self, copilot, capsys):
        from hooks.targets import emitter

        emitter.buffer_context("x" * 20000)
        emitter.flush("SessionStart")
        payload = json.loads(capsys.readouterr().out)
        assert len(payload["additionalContext"]) == 20000

    def test_codex_has_no_cap(self, codex, capsys):
        from hooks.targets import emitter

        emitter.buffer_context("x" * 20000)
        emitter.flush("PostToolUse")
        payload = json.loads(capsys.readouterr().out)
        assert len(payload["hookSpecificOutput"]["additionalContext"]) == 20000


class TestCodexToolNames:
    """Codex 0.154 hands the hook boundary Claude's own names for the shell tool
    (captured live: tool_name "Bash", tool_input {"command": "<string>"}) but keeps
    its own for the patch tool, which reaches no Write/Edit branch unmapped."""

    def test_shell_payload_passes_through(self, codex):
        payload = normalize_payload({"tool_name": "Bash", "tool_input": {"command": "ls -la"}})
        assert payload["tool_name"] == "Bash"
        assert payload["tool_input"] == {"command": "ls -la"}

    def test_apply_patch_reaches_the_edit_branch(self, codex):
        body = "*** Begin Patch\n*** Update File: /repo/pyproject.toml\n@@\n+x\n*** End Patch"
        payload = normalize_payload({"tool_name": "apply_patch", "tool_input": {"command": body}})
        assert payload["tool_name"] == "Edit"
        assert payload["tool_input"]["content"] == body
        assert payload["tool_input"]["new_string"] == body
        assert payload["tool_input"]["file_path"] == "/repo/pyproject.toml"

    def test_model_facing_shell_names_map_to_bash(self, codex):
        for name in ("exec", "shell", "local_shell", "unified_exec"):
            assert normalize_payload({"tool_name": name, "tool_input": {}})["tool_name"] == "Bash"

    def test_list_command_becomes_a_string(self, codex):
        payload = normalize_payload({"tool_name": "shell", "tool_input": {"command": ["ls", "a b"]}})
        assert payload["tool_input"]["command"] == "ls 'a b'"

    def test_freeform_program_input_reaches_the_command_guards(self, codex):
        program = 'const r = await tools.exec_command({cmd: "sudo rm -rf /srv"});'
        payload = normalize_payload({"tool_name": "exec", "tool_input": {"input": program}})
        assert payload["tool_input"]["command"] == program

    def test_unmapped_name_passes_through(self, codex):
        payload = normalize_payload({"tool_name": "update_plan", "tool_input": {"plan": []}})
        assert payload["tool_name"] == "update_plan"

    def test_claude_payload_still_untouched(self, claude):
        payload = {"tool_name": "apply_patch", "tool_input": {"command": "x"}}
        assert normalize_payload(dict(payload)) == payload


class TestCapabilities:
    def test_codex_pretooluse_has_no_context_channel(self):
        assert can_inject_context("PreToolUse", target="codex") is False

    def test_codex_other_events_inject(self):
        for event in ("SessionStart", "UserPromptSubmit", "PostToolUse", "Stop"):
            assert can_inject_context(event, target="codex") is True

    def test_claude_injects_everywhere(self):
        assert can_inject_context("PreToolUse", target="claude") is True

    def test_codex_permission_decisions_deny_only(self):
        assert allowed_permission_decisions("codex") == frozenset({"deny"})
        assert "allow" not in allowed_permission_decisions("codex")

    def test_claude_permission_decisions_full(self):
        assert allowed_permission_decisions("claude") == frozenset({"allow", "deny", "ask"})


class TestEmitter:
    def test_flush_empty_is_silent(self, codex, capsys):
        flush("PostToolUse")
        assert capsys.readouterr().out == ""

    def test_codex_flush_single_envelope(self, codex, capsys):
        buffer_context("first block")
        buffer_context("second block")
        flush("PostToolUse")
        out = capsys.readouterr().out.strip().splitlines()
        assert len(out) == 1, "codex must receive exactly one JSON line"
        doc = json.loads(out[0])
        ctx = doc["hookSpecificOutput"]["additionalContext"]
        assert "first block" in ctx and "second block" in ctx
        assert doc["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
        assert not has_buffered(), "flush must clear the buffer"

    def test_codex_pretooluse_flush_drops(self, codex, capsys):
        buffer_context("advisory note")
        flush("PreToolUse")
        assert capsys.readouterr().out == ""
        assert not has_buffered()

    def test_inject_context_buffers_on_codex(self, codex, capsys):
        from hooks.common import inject_context

        inject_context("hello codex", also_log=False, skip_compression=True)
        assert capsys.readouterr().out == ""
        assert has_buffered()
        flush("SessionStart")
        doc = json.loads(capsys.readouterr().out)
        assert "hello codex" in doc["hookSpecificOutput"]["additionalContext"]

    def test_inject_context_prints_on_claude(self, claude, capsys):
        from hooks.common import inject_context

        inject_context("hello claude", also_log=False, skip_compression=True)
        out = capsys.readouterr().out
        assert "=== CONTEXT INJECTION ===" in out
        assert "hello claude" in out
        assert not has_buffered()

    def test_drain_returns_joined_content_and_clears(self, codex, capsys):
        buffer_context("first block")
        buffer_context("second block")
        content = drain()
        assert "first block" in content and "second block" in content
        assert not has_buffered()
        # drain() never emits anything itself — unlike flush(), it's silent.
        assert capsys.readouterr().out == ""

    def test_drain_empty_is_safe(self, codex, capsys):
        assert not has_buffered()
        assert drain() == ""
        assert capsys.readouterr().out == ""

    def test_drain_after_block_prevents_leak_into_next_event(self, codex, capsys):
        """The invariant fix_2/fix_3 exist to guarantee: content buffered for
        one event must never survive into the next event's flush."""
        buffer_context("event-one context")
        drain()  # simulates main()'s BlockAction/finally path
        flush("PostToolUse")  # a later event in the same (hypothetical) process
        assert capsys.readouterr().out == "", "drained buffer leaked into a later flush"


class TestPermissionDecisionChokePoint:
    """hooks.hook_manager.emit_permission_decision is the sole legal call
    site for a permissionDecision envelope. It must filter every decision
    through allowed_permission_decisions() so a future non-deny decision
    cannot leak through on codex, even though nothing emits one today."""

    def test_codex_deny_is_emitted(self, codex, capsys):
        from hooks.hook_manager import emit_permission_decision

        emit_permission_decision("PreToolUse", "deny", reason="blocked")
        out = capsys.readouterr().out.strip()
        doc = json.loads(out)
        assert doc["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert doc["hookSpecificOutput"]["permissionDecisionReason"] == "blocked"

    def test_codex_allow_is_dropped_not_printed(self, codex, capsys):
        from hooks.hook_manager import emit_permission_decision

        emit_permission_decision("PreToolUse", "allow")
        assert capsys.readouterr().out == ""

    def test_codex_ask_is_dropped_not_printed(self, codex, capsys):
        from hooks.hook_manager import emit_permission_decision

        emit_permission_decision("PreToolUse", "ask")
        assert capsys.readouterr().out == ""

    def test_claude_allow_is_emitted(self, claude, capsys):
        from hooks.hook_manager import emit_permission_decision

        emit_permission_decision("PreToolUse", "allow")
        out = capsys.readouterr().out.strip()
        doc = json.loads(out)
        assert doc["hookSpecificOutput"]["permissionDecision"] == "allow"


class TestGlobalRecord:
    """hooks.targets.global_record — statusline/enforcement read state.json
    directly and must tolerate both the keyed and legacy flat shapes."""

    KEYED = {
        "targets": {
            "global": {
                "claude": {"profile": "anton,brain", "settings_profile": "sp1"},
                "codex": {"profile": "anton"},
            }
        }
    }
    LEGACY = {"targets": {"global": {"path": "/x", "profile": "anton,brain"}}}

    def test_keyed_shape_claude(self, claude):
        from hooks.targets import global_record

        assert global_record(self.KEYED)["profile"] == "anton,brain"

    def test_keyed_shape_codex(self, codex):
        from hooks.targets import global_record

        assert global_record(self.KEYED)["profile"] == "anton"

    def test_legacy_flat_shape(self, claude):
        from hooks.targets import global_record

        assert global_record(self.LEGACY)["profile"] == "anton,brain"

    def test_missing_target_record_empty(self, codex):
        from hooks.targets import global_record

        assert global_record({"targets": {"global": {"claude": {"profile": "p"}}}}) == {}

    def test_empty_state(self, claude):
        from hooks.targets import global_record

        assert global_record({}) == {}


class TestCodexTranscriptResolution:
    """Codex omits transcript_path; the normalizer resolves the rollout by
    session id so brain markers / auto-save / tool-memory keep working."""

    def _rollout(self, home, sid, day="2026/08/10"):
        d = home / "sessions" / day
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"rollout-2026-08-10T21-30-30-{sid}.jsonl"
        f.write_text('{"type":"session_meta","payload":{}}\n')
        return f

    def test_resolves_rollout_from_session_id(self, codex, tmp_path, monkeypatch):
        from hooks.targets.normalizer import normalize_payload

        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        sid = "019fed27-b61d-7a92-bcec-4ffcccf53b71"
        expected = self._rollout(tmp_path, sid)
        out = normalize_payload({"hook_event_name": "SessionEnd", "session_id": sid})
        assert out["transcript_path"] == str(expected)

    def test_existing_transcript_path_wins(self, codex, tmp_path, monkeypatch):
        from hooks.targets.normalizer import normalize_payload

        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        sid = "abc-123"
        self._rollout(tmp_path, sid)
        out = normalize_payload({"session_id": sid, "transcript_path": "/given/path.jsonl"})
        assert out["transcript_path"] == "/given/path.jsonl"

    def test_no_match_leaves_key_absent(self, codex, tmp_path, monkeypatch):
        from hooks.targets.normalizer import normalize_payload

        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        out = normalize_payload({"session_id": "nothing-here"})
        assert not out.get("transcript_path")

    def test_claude_payload_untouched(self, claude, tmp_path, monkeypatch):
        from hooks.targets.normalizer import normalize_payload

        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        sid = "xyz-789"
        self._rollout(tmp_path, sid)
        out = normalize_payload({"session_id": sid})
        assert "transcript_path" not in out


class TestCodexRolloutResolverHardening:
    """A hook that raises here skips every guardrail for that event, and a
    lookup charged to every tool call taxes the whole turn."""

    def test_glob_metachars_never_raise(self, codex, tmp_path, monkeypatch):
        from hooks.targets.normalizer import codex_rollout_path

        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        (tmp_path / "sessions" / "2026" / "08" / "10").mkdir(parents=True)
        for evil in ("**", "*", "?", "[a-z]", "../../etc", "a" * 500, ""):
            assert codex_rollout_path(evil) == ""

    def test_metachar_id_cannot_match_another_session(self, codex, tmp_path, monkeypatch):
        from hooks.targets.normalizer import codex_rollout_path

        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        d = tmp_path / "sessions" / "2026" / "08" / "10"
        d.mkdir(parents=True)
        real = "019fed35-c6df-7a43-a079-562b82daf86a"
        (d / f"rollout-2026-08-10T21-45-52-{real}.jsonl").write_text("{}\n")
        # One character replaced by a wildcard must NOT resolve to the real file.
        assert codex_rollout_path(real[:-1] + "?") == ""
        assert codex_rollout_path(real).endswith(f"{real}.jsonl")

    def test_tool_events_do_not_pay_for_resolution(self, codex, tmp_path, monkeypatch):
        """PreToolUse/PostToolUse must not touch the filesystem for a value
        their handlers never read — that cost is per tool call."""
        from hooks.targets import normalizer

        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        calls = []
        monkeypatch.setattr(normalizer, "codex_rollout_path", lambda sid: calls.append(sid) or "")
        for event in ("PreToolUse", "PostToolUse", "UserPromptSubmit", "SessionStart"):
            normalizer.normalize_payload({"hook_event_name": event, "session_id": "abc"})
        assert calls == []
        for event in ("SessionEnd", "Stop", "SubagentStop", "PreCompact"):
            normalizer.normalize_payload({"hook_event_name": event, "session_id": "abc"})
        assert len(calls) == 4

    def test_deep_history_stays_fast(self, codex, tmp_path, monkeypatch):
        """Resolution must not scale with total session history."""
        import time

        from hooks.targets.normalizer import codex_rollout_path

        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        sessions = tmp_path / "sessions"
        for month in range(1, 9):  # 8 months of history
            for day in range(1, 29):
                d = sessions / "2026" / f"{month:02d}" / f"{day:02d}"
                d.mkdir(parents=True)
                for n in range(10):
                    (d / f"rollout-2026-{month:02d}-{day:02d}T00-00-{n:02d}-old{month}{day}{n}.jsonl").touch()
        sid = "019fed99-aaaa-bbbb-cccc-ddddeeeeffff"
        newest = sessions / "2026" / "08" / "28"
        (newest / f"rollout-2026-08-28T12-00-00-{sid}.jsonl").write_text("{}\n")
        start = time.perf_counter()
        for _ in range(20):
            assert codex_rollout_path(sid)
        elapsed = (time.perf_counter() - start) / 20
        # A full-tree walk of ~2200 files costs milliseconds per call; the
        # newest-first scan is bounded by one day directory.
        assert elapsed < 0.005, f"{elapsed * 1000:.2f} ms/call — resolution is walking history"


class TestSessionIdBannerHost:
    """The banner names the host; saying 'Claude Code' inside codex is a lie."""

    def _banner(self, monkeypatch, target):
        monkeypatch.setenv("AGENTIHOOKS_TARGET", target)
        captured = []
        import hooks.common as common

        monkeypatch.setattr(common, "inject_context", lambda msg, *a, **k: captured.append(msg))
        from hooks.hook_manager import on_session_start

        try:
            on_session_start({"hook_event_name": "SessionStart", "session_id": "sid-1", "cwd": "/tmp"})
        except Exception:
            pass
        return "\n".join(captured)

    def test_codex_banner_names_codex(self, monkeypatch):
        assert "Your Codex session_id" in self._banner(monkeypatch, "codex")

    def test_claude_banner_unchanged(self, monkeypatch):
        assert "Your Claude Code session_id" in self._banner(monkeypatch, "claude")


class TestPreToolUseLogAttribution:
    """The hook log is machine-global; a line with no session_id cannot be
    attributed to the session that wrote it (PostToolUse already carries one)."""

    def test_pre_tool_use_log_carries_session_id(self, claude, monkeypatch):
        import hooks.hook_manager as hm

        seen = []
        monkeypatch.setattr(hm, "log", lambda msg, payload=None: seen.append((msg, payload or {})))
        try:
            hm.on_pre_tool_use(
                {
                    "hook_event_name": "PreToolUse",
                    "session_id": "sid-attribution",
                    "tool_name": "Bash",
                    "tool_input": {"command": "echo hi"},
                    "cwd": "/tmp",
                }
            )
        except Exception:
            pass
        pre = [p for m, p in seen if m.startswith("Pre tool use")]
        assert pre, "no Pre tool use line logged"
        assert pre[0].get("session_id") == "sid-attribution"


class TestCodexEnforcementFallback:
    def test_posttool_injects_due_enforcement_with_project_cwd(self, codex, monkeypatch):
        import hooks.common as common
        import hooks.context.enforcement as enforcement
        import hooks.hook_manager as hm

        captured = []
        calls = []
        monkeypatch.setattr(common, "inject_context", lambda message, **kwargs: captured.append(message))
        monkeypatch.setattr(
            enforcement,
            "get_posttool_enforcements",
            lambda session_id, cwd: calls.append((session_id, cwd)) or "local enforcement",
        )
        hm.on_post_tool_use(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "codex-local",
                "tool_name": "Unknown",
                "tool_input": {},
                "tool_output": "ok",
                "cwd": "/project",
            }
        )
        assert calls == [("codex-local", "/project")]
        assert captured == ["local enforcement"]


class TestSplitGlobal:
    """The one shape rule shared by the installer's migration and the hook
    runtime, which never runs that migration."""

    KEYED = {"claude": {"profile": "anton"}, "codex": {"profile": "smith"}}
    FLAT = {"path": "/home/x/.claude", "profile": "anton"}
    MIXED = {"path": "/home/x/.claude", "profile": "anton", "codex": {"profile": "smith"}}

    def test_keyed_passes_through(self):
        assert split_global(self.KEYED) == self.KEYED

    def test_flat_nests_under_claude(self):
        assert split_global(self.FLAT) == {"claude": self.FLAT}

    def test_mixed_keeps_records_and_nests_flat_fields(self):
        assert split_global(self.MIXED) == {
            "claude": {"path": "/home/x/.claude", "profile": "anton"},
            "codex": {"profile": "smith"},
        }

    def test_flat_fields_win_over_existing_claude_record(self):
        g = {"claude": {"profile": "old", "settings_profile": "lean"}, "profile": "new"}
        assert split_global(g) == {"claude": {"profile": "new", "settings_profile": "lean"}}

    def test_junk_is_empty(self):
        assert split_global(None) == {}
        assert split_global({}) == {}
        assert split_global("nonsense") == {}

    def test_never_mutates_input(self):
        g = dict(self.MIXED)
        split_global(g)
        assert g == self.MIXED

    def test_mixed_shape_does_not_leak_sibling_into_current_target(self, codex):
        """The old any-non-dict heuristic handed back the whole blob, so the
        codex record arrived carrying claude's fields and a nested 'codex' key."""
        rec = global_record({"targets": {"global": dict(self.MIXED)}})
        assert rec == {"profile": "smith"}

    def test_mixed_shape_claude_record_is_the_flat_fields(self, claude):
        rec = global_record({"targets": {"global": dict(self.MIXED)}})
        assert rec == {"path": "/home/x/.claude", "profile": "anton"}


class TestCopilotNormalizer:
    def test_camelcase_keys_filled_to_snake_case(self, copilot):
        payload = normalize_payload(
            {
                "hookEventName": "preToolUse",
                "sessionId": "s1",
                "toolName": "shell",
                "toolArgs": {"command": "ls"},
            }
        )
        assert payload["hook_event_name"] == "PreToolUse"
        assert payload["session_id"] == "s1"
        assert payload["tool_name"] == "Bash", "copilot tool names map onto the guardrail vocabulary"
        assert payload["tool_input"] == {"command": "ls"}

    def test_event_names_map_to_dispatch_vocabulary(self, copilot):
        from hooks.hook_manager import EVENT_HANDLERS

        for raw, expected in (
            ("sessionStart", "SessionStart"),
            ("sessionEnd", "SessionEnd"),
            ("userPromptSubmitted", "UserPromptSubmit"),
            ("preToolUse", "PreToolUse"),
            ("postToolUse", "PostToolUse"),
            ("postToolUseFailure", "PostToolUse"),
            ("agentStop", "Stop"),
            ("subagentStop", "SubagentStop"),
            ("preCompact", "PreCompact"),
            ("permissionRequest", "PermissionRequest"),
            ("notification", "Notification"),
        ):
            payload = normalize_payload({"hookEventName": raw})
            assert payload["hook_event_name"] == expected
            assert expected in EVENT_HANDLERS, f"{raw} maps to an undispatched event"

    def test_every_registered_event_resolves_to_a_handler(self, copilot):
        """An unmapped name reaches no handler and exits 0 — a silent bypass of
        every guardrail for that event."""
        from hooks.hook_manager import EVENT_HANDLERS
        from scripts.targets.copilot_target import COPILOT_HOOK_EVENTS

        for registered in COPILOT_HOOK_EVENTS:
            payload = normalize_payload({"hookEventName": registered})
            resolved = payload.get("hook_event_name")
            assert resolved in EVENT_HANDLERS, f"{registered} resolves to {resolved!r}, which dispatches nothing"

    def test_every_camelcase_value_has_a_pascalcase_identity(self, copilot):
        from hooks.targets.normalizer import _COPILOT_EVENTS

        for dispatch_name in set(_COPILOT_EVENTS.values()):
            assert _COPILOT_EVENTS.get(dispatch_name) == dispatch_name

    def test_present_but_empty_key_does_not_shadow_the_real_value(self, copilot):
        """setdefault would keep the empty dict and hand guardrails blank args
        while the tool call still executes."""
        payload = normalize_payload(
            {
                "hookEventName": "preToolUse",
                "toolName": "Bash",
                "toolArgs": {"command": "rm -rf /"},
                "tool_input": {},
                "tool_name": "",
            }
        )
        assert payload["tool_input"] == {"command": "rm -rf /"}
        assert payload["tool_name"] == "Bash"

    def test_pascalcase_alias_passes_through(self, copilot):
        """Copilot accepts the claude-style names at registration and may echo them."""
        payload = normalize_payload({"hook_event_name": "SessionStart", "session_id": "s"})
        assert payload["hook_event_name"] == "SessionStart"

    def test_original_event_name_preserved_for_diagnostics(self, copilot):
        payload = normalize_payload({"hookEventName": "postToolUseFailure"})
        assert payload["copilot_event_name"] == "postToolUseFailure"

    def test_result_aliases_filled(self, copilot):
        payload = normalize_payload({"hookEventName": "postToolUse", "toolResult": {"out": 1}})
        assert payload["tool_response"] == {"out": 1}
        assert payload["tool_output"] == {"out": 1}

    def test_claude_payload_untouched_under_copilot_is_not_assumed(self, claude):
        payload = {"hookEventName": "preToolUse", "toolName": "shell"}
        assert normalize_payload(dict(payload)) == payload


class TestCopilotCapabilities:
    def test_all_three_permission_decisions(self):
        assert allowed_permission_decisions("copilot") == frozenset({"allow", "deny", "ask"})

    def test_context_channel_open_on_every_event(self):
        for event in ("PreToolUse", "SessionStart", "UserPromptSubmit", "PostToolUse", "Stop"):
            assert can_inject_context(event, target="copilot") is True

    def test_arg_mutation_targets(self):
        assert supports_arg_mutation("copilot") is True
        assert supports_arg_mutation("codex") is False
        assert supports_arg_mutation("claude") is True

    def test_envelope_block_required_only_for_copilot(self):
        # Settled live on v1.0.80: preToolUse denies on exit 2 alone;
        # userPromptSubmitted blocks ONLY via a stdout {"decision": "block"}.
        assert requires_envelope_block("PreToolUse", "copilot") is True
        assert requires_envelope_block("PermissionRequest", "copilot") is True
        assert requires_envelope_block("UserPromptSubmit", "copilot") is True
        assert requires_envelope_block("PreToolUse", "codex") is False
        assert requires_envelope_block("PreToolUse", "claude") is False

    def test_envelope_block_not_emitted_where_there_is_no_decision_field(self):
        for event in ("SessionStart", "SessionEnd", "Stop", "Notification"):
            assert requires_envelope_block(event, "copilot") is False


class TestCopilotBuiltinWriteToolMapping:
    def test_apply_patch_maps_to_edit_with_patch_in_content(self, copilot):
        payload = normalize_payload(
            {"toolCalls": [{"id": "a", "name": "apply_patch", "args": '{"patch": "diff body"}'}]}
        )
        assert payload["tool_name"] == "Edit"
        assert payload["tool_input"]["content"] == "diff body"

    def test_str_replace_editor_maps_by_nested_command(self, copilot):
        for cmd, expected in (("create", "Write"), ("str_replace", "Edit"), ("insert", "Edit"), ("view", "Read")):
            payload = normalize_payload(
                {
                    "toolCalls": [
                        {"id": "a", "name": "str_replace_editor", "args": f'{{"command": "{cmd}", "path": "/x"}}'}
                    ]
                }
            )
            assert payload["tool_name"] == expected, f"{cmd} should map to {expected}"
