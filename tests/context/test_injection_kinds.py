import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytestmark = pytest.mark.unit

QUIET = {"also_log": False, "skip_compression": True}
BRAIN = "BRAIN\n" + "\n".join("arc " + "b" * 396 for _ in range(30))


def _record(monkeypatch) -> list:
    calls = []
    monkeypatch.setattr("hooks.common.inject_context", lambda *a, **k: calls.append((a, k)))
    return calls


def test_inbox_refocus_ledger_and_quota_helpers_tag_their_kind(monkeypatch):
    from hooks import hook_manager

    calls = _record(monkeypatch)
    inbox = Mock(return_value=["mail"])
    monkeypatch.setattr(hook_manager, "_inbox_blocks", inbox)
    monkeypatch.setattr(hook_manager, "_refocus_blocks", Mock(return_value=["focus"]))
    waiting = Mock(return_value=["wait"])
    monkeypatch.setattr(hook_manager, "_wait_nudge_blocks", waiting)
    monkeypatch.setattr("hooks.context.ledger_decision.directive", Mock(return_value="decision"))
    monkeypatch.setattr("hooks.context.quota_policy.posttool_context", Mock(return_value="quota"))

    hook_manager._inject_ledger_decision({"session_id": "s"})
    hook_manager._inject_after_tool({"session_id": "s", "cwd": "/w", "tool_name": "Bash"})

    assert calls == [
        (("decision",), {**QUIET, "kind": "alert"}),
        (("mail",), {**QUIET, "kind": "inbox"}),
        (("focus",), {**QUIET, "kind": "refocus"}),
        (("wait",), {**QUIET, "kind": "refocus"}),
        (("quota",), {**QUIET, "kind": "alert"}),
    ]
    inbox.assert_called_once_with("s", "/w")
    waiting.assert_called_once_with({"session_id": "s", "cwd": "/w", "tool_name": "Bash"})


def test_prompt_submit_tags_policy_secret_warning_and_enforcements(monkeypatch):
    from hooks import config, hook_manager

    calls = _record(monkeypatch)
    monkeypatch.setattr(config, "SECRETS_MODE", "warn")
    monkeypatch.setattr(config, "QUOTA_POLICY_ENABLED", True)
    for flag in (
        "QUOTA_USAGE_INJECTION_ENABLED",
        "CI_MANIFESTO_ENABLED",
        "VOICE_ENABLED",
        "CONTROLS_BYPASS_ENABLED",
        "BROADCAST_ENABLED",
        "AMYGDALA_ENABLED",
    ):
        monkeypatch.setattr(config, flag, False)
    for name in (
        "_confirm_inbox",
        "_swarm_heartbeat",
        "_swarm_ledger",
        "_operator_mode",
        "_master_coding",
        "_request_trace_flush",
        "_inject_refocus",
        "_inject_ledger_decision",
    ):
        monkeypatch.setattr(hook_manager, name, Mock())
    monkeypatch.setattr(hook_manager, "_operator_words", Mock(return_value=False))
    monkeypatch.setattr("hooks.context.rules_refresh.maybe_inject", Mock())
    monkeypatch.setattr("hooks.context.quota_policy.record_push_signal", Mock(return_value=False))
    monkeypatch.setattr("hooks.context.quota_policy.prompt_context", Mock(return_value="policy"))
    monkeypatch.setattr("hooks.secrets.scan", Mock(return_value=["aws_access_key"]))
    monkeypatch.setattr("hooks.context.enforcement.get_user_prompt_enforcements", Mock(return_value="rules"))

    hook_manager.on_user_prompt_submit({"session_id": "s", "prompt": "hello", "cwd": ""})

    assert (("policy",), {**QUIET, "kind": "alert"}) in calls
    warning = (
        "WARNING: Possible secret(s) detected in prompt (aws_access_key). "
        "Do not process, echo, or store credential values. "
        "Ask the user to use environment variables instead."
    )
    assert ((warning,), {"kind": "alert"}) in calls
    assert (("rules",), {**QUIET, "kind": "enforcement"}) in calls


def _quiet_pretool(monkeypatch, *, policy: bool):
    from hooks import config, hook_manager
    from hooks.targets import emitter

    monkeypatch.setenv("AGENTIHOOKS_TARGET", "claude")
    monkeypatch.setattr(emitter, "_forced", True)
    monkeypatch.setattr(config, "SECRETS_MODE", "off")
    monkeypatch.setattr(config, "RETRY_BREAKER_ENABLED", False)
    for flag in (
        "BRAIN_ENABLED",
        "BROADCAST_ENABLED",
        "ENFORCEMENT_INJECTION_ENABLED",
        "QUOTA_USAGE_INJECTION_ENABLED",
    ):
        monkeypatch.setattr(config, flag, True)
    monkeypatch.setattr(config, "QUOTA_POLICY_ENABLED", policy)
    monkeypatch.setattr("hooks.context.quota_usage.QUOTA_USAGE_TOOL_CALLS", 1)
    monkeypatch.setattr(config, "QUOTA_USAGE_TOOL_CALLS", 1)
    monkeypatch.setattr("hooks.context.quota_policy.pretool", Mock(return_value=(None, "POLICY")))
    monkeypatch.setattr("hooks.context.quota_usage.quota_banner", Mock(return_value="BANNER"))
    monkeypatch.setattr("hooks.context.enforcement.increment_and_get_count", Mock(return_value=1))
    monkeypatch.setattr("hooks.context.context_recycle.directive", Mock(return_value="RECYCLE"))
    monkeypatch.setattr("hooks.lifecycle.guard.pretool", Mock(return_value=None))
    monkeypatch.setattr("hooks.tool_memory.inject_memory", Mock())
    monkeypatch.setattr("hooks.context.brain_adapter.maybe_refresh_on_tool_call", Mock(return_value=BRAIN))
    monkeypatch.setattr("hooks.context.broadcast.get_pretool_context", Mock(return_value="  - [CRITICAL] (id:b1) CAST"))
    monkeypatch.setattr("hooks.context.enforcement.get_pretool_enforcements", Mock(return_value="RULES"))
    monkeypatch.setattr(
        "hooks.context.conditions.pre_effect",
        Mock(return_value=SimpleNamespace(contexts=["COND"], block=None, rewrite=None, decision=None, reason="")),
    )
    monkeypatch.setattr(hook_manager, "_operator_question", Mock(return_value=""))
    monkeypatch.setattr(hook_manager, "_operator_reminder", Mock())
    monkeypatch.setattr(hook_manager, "_swarm_heartbeat", Mock())
    monkeypatch.setattr(hook_manager, "_refocus_blocks", Mock(return_value=["FOCUS"]))
    monkeypatch.setattr(hook_manager, "_wait_nudge_blocks", Mock(return_value=["NUDGE"]))
    inbox = Mock(return_value=["MAIL"])
    monkeypatch.setattr(hook_manager, "_inbox_blocks", inbox)
    return hook_manager, inbox


@pytest.mark.parametrize(
    ("policy", "payload_cwd", "cwd", "middle"),
    [
        (True, {"cwd": "/w"}, "/w", ["RECYCLE", "POLICY", "  - [CRITICAL] (id:b1) CAST"]),
        (False, {}, "", ["RECYCLE", "  - [CRITICAL] (id:b1) CAST"]),
    ],
)
def test_tool_call_blocks_rank_by_kind(monkeypatch, capsys, policy, payload_cwd, cwd, middle):
    hook_manager, inbox = _quiet_pretool(monkeypatch, policy=policy)

    hook_manager.on_pre_tool_use({"session_id": "s", "tool_name": "Unknown", "tool_input": {}, **payload_cwd})

    context = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    banner = [] if policy else ["BANNER"]
    head = ["MAIL", *middle, "RULES", "=== CONTEXT INJECTION ===\nCOND", "FOCUS", "NUDGE", *banner, "BRAIN"]
    assert context.startswith("\n\n".join(head) + "\narc ")
    assert context.endswith("\n\n[context budget: shortened brain feed and broadcasts to stay under 9000 characters]")
    inbox.assert_called_once_with("s", cwd)


def test_pretool_condition_context_is_quiet_and_tagged(monkeypatch, capsys):
    hook_manager, _ = _quiet_pretool(monkeypatch, policy=False)
    calls = _record(monkeypatch)

    hook_manager.on_pre_tool_use({"session_id": "s", "tool_name": "Unknown", "tool_input": {}})

    assert calls[0] == (("COND",), {**QUIET, "kind": "condition"})


def test_posttool_condition_context_is_quiet_and_tagged(monkeypatch):
    from hooks import hook_manager

    calls = _record(monkeypatch)
    monkeypatch.setattr(
        "hooks.context.conditions.post_effect",
        Mock(return_value=SimpleNamespace(contexts=["PCOND"], hook_fields={}, top_fields={})),
    )

    hook_manager.on_post_tool_use({"session_id": "s", "tool_name": "Unknown", "tool_input": {}, "tool_response": {}})

    assert calls[0] == (("PCOND",), {**QUIET, "kind": "condition"})


@pytest.mark.parametrize(
    ("event", "forced", "plain"),
    [
        ("SessionStart", True, True),
        ("UserPromptSubmit", True, True),
        ("PreToolUse", True, False),
        ("PostToolUse", True, False),
        ("Stop", False, False),
    ],
)
def test_main_buffers_claude_context_events(monkeypatch, event, forced, plain):
    import io

    from hooks import hook_manager
    from hooks.targets import emitter

    monkeypatch.setenv("AGENTIHOOKS_TARGET", "claude")
    monkeypatch.delenv("AGENTIHOOKS_CLASSIFIER_CHILD", raising=False)
    seen = {}
    handler = Mock(side_effect=lambda payload: seen.update(forced=emitter.forced(), plain=emitter._plain))
    monkeypatch.setattr(hook_manager.sys, "stdin", io.StringIO(json.dumps({"hook_event_name": event})))
    monkeypatch.setitem(hook_manager.EVENT_HANDLERS, event, handler)
    monkeypatch.setattr(hook_manager.otel, "flush", Mock())
    monkeypatch.setattr(hook_manager.os, "_exit", Mock())

    hook_manager.main()

    assert seen == {"forced": forced, "plain": plain}


def test_banner_defaults_and_passes_its_flags(monkeypatch):
    from hooks.common import inject_banner

    calls = _record(monkeypatch)
    inject_banner("T", "body")
    inject_banner("T", "body", also_log=False, skip_compression=True, kind="brain")

    assert calls[0][1] == {"also_log": True, "skip_compression": False, "kind": "other"}
    assert calls[1][1] == {**QUIET, "kind": "brain"}
    assert "║  body" in calls[0][0][0]


def test_buffer_defaults_to_notice_kind_and_drain_joins_lines():
    from hooks.targets import emitter

    emitter.buffer_context("one")
    assert emitter.drain_blocks() == [("other", "one")]
    emitter.buffer_context("a")
    emitter.buffer_context("b", "brain")
    assert emitter.drain() == "a\nb"


def test_auto_dev_switch_buffers_when_claude_forces_buffering(monkeypatch, capsys):
    from hooks.context import auto_dev_switch
    from hooks.targets import emitter

    monkeypatch.setenv("AGENTIHOOKS_TARGET", "claude")
    emitter.force_buffer(plain=True)
    monkeypatch.setattr(auto_dev_switch, "ensure_on_dev", Mock(return_value="switched"))

    auto_dev_switch.inject_on_session_start("/w")

    assert capsys.readouterr().out == ""
    assert emitter.drain_blocks() == [("other", "[auto-dev-switch] switched")]


def test_runtime_manifesto_buffers_when_claude_forces_buffering(monkeypatch, capsys):
    from hooks import config
    from hooks.context import ci_manifesto
    from hooks.targets import emitter

    monkeypatch.setenv("AGENTIHOOKS_TARGET", "claude")
    emitter.force_buffer(plain=True)
    monkeypatch.setattr(config, "CI_MANIFESTO_ENABLED", True)
    monkeypatch.setattr(config, "CI_MANIFESTO_RUNTIME_INJECT", True)
    monkeypatch.setattr(config, "CI_MANIFESTO_REFRESH_EVERY", 1)
    monkeypatch.setattr(ci_manifesto, "_build_injection", Mock(return_value="doctrine"))

    ci_manifesto.inject_on_session_start()
    ci_manifesto.maybe_refresh("s")

    assert capsys.readouterr().out == ""
    assert emitter.drain_blocks() == [("other", "doctrine"), ("other", "doctrine")]


def test_tool_memory_header_and_exact_length_entry(monkeypatch, tmp_path):
    from hooks import tool_memory

    error = "e" * (tool_memory.ENTRY_CHARS - len("- [2026-10-15 17:10] Bash: "))
    memory = tmp_path / "memory.ndjson"
    memory.write_text(json.dumps({"ts": "2026-10-15T17:10:00Z", "tool": "Bash", "error": error}) + "\n")
    monkeypatch.setattr(tool_memory, "MEMORY_PATH", memory)
    calls = _record(monkeypatch)

    tool_memory.inject_memory()

    assert calls[0][0][0] == f"TOOL MEMORY: lessons from past sessions\n- [2026-10-15 17:10] Bash: {error}"


def test_shorten_keeps_whole_lines_then_whole_characters():
    from hooks.context.injection_budget import _shorten

    assert _shorten("aa\nbb\ncc", 5) == "aa\nbb"
    assert _shorten("aa\nbb\ncc", 4) == "aa"
    assert _shorten("aa\nbb\ncc", 1) == "a"
    assert _shorten("a\U0001f7e3\U0001f7e3", 3) == "a\U0001f7e3"
    assert _shorten("a\U0001f7e3\U0001f7e3", 2) == "a"
    assert _shorten("abc", -3) == ""


def test_exact_budget_is_kept_without_a_notice():
    from hooks.context.injection_budget import fit

    assert fit([("alert", "a" * 10), ("brain", "b" * 9)], 20) == "a" * 10 + "\n" + "b" * 9


def test_block_that_exactly_fills_the_room_stays_whole():
    from hooks.context.injection_budget import fit

    lines = "a" * 399 + "\n" + "a" * 400
    out = fit([("alert", lines), ("brain", "b" * 300)], 1000)
    assert out == lines + "\n[context budget: left out brain feed and broadcasts to stay under 1000 characters]"


def test_units_counts_whole_utf16_code_units():
    from hooks.context.injection_budget import units

    assert units("a\U0001f7e3") == 3
    assert isinstance(units("ab"), int)


def test_second_block_pays_for_its_separator():
    from hooks.context.injection_budget import fit

    out = fit([("alert", "a" * 400), ("enforcement", "r" * 399), ("brain", "b" * 300)], 1000)
    assert out.startswith("a" * 400 + "\n" + "r" * 399 + "\n[context budget: left out brain")
    out = fit([("alert", "a" * 400), ("enforcement", "r" * 400), ("brain", "b" * 300)], 1000)
    assert out.startswith("a" * 400 + "\n" + "r" * 399 + "\n[context budget: left out brain")
    assert "shortened enforcements" in out


@pytest.mark.parametrize(("lead", "kept"), [(549, 250), (599, 0)])
def test_short_remainders_follow_the_minimum(lead, kept):
    from hooks.context.injection_budget import fit

    out = fit([("alert", "a" * lead), ("mystery", "b" * 501)], 1000)
    if kept:
        assert (
            out
            == "a" * lead + "\n" + "b" * kept + "\n[context budget: shortened notices to stay under 1000 characters]"
        )
    else:
        assert out == "a" * lead + "\n[context budget: left out notices to stay under 1000 characters]"


def test_notice_lists_dropped_and_every_shortened_kind():
    from hooks.context.injection_budget import fit

    first = "x" * 500 + "\n" + "x" * 500
    second = "\n".join("y" * 20 for _ in range(20))
    out = fit([("alert", first), ("brain", second), ("tool_memory", "t" * 400)], 1000)
    assert out.endswith(
        "\n[context budget: left out tool memory; shortened alerts, brain feed and broadcasts"
        " to stay under 1000 characters]"
    )
    assert out.startswith("x" * 500 + "\n" + "y" * 20)


def test_budget_of_one_character_still_applies(monkeypatch):
    from hooks import config
    from hooks.context.injection_budget import budget_chars

    monkeypatch.setattr(config, "HOOK_INJECTION_MAX_CHARS", 1)
    assert budget_chars("claude") == 1
