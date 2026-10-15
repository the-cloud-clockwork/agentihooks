import json
from unittest.mock import Mock

import pytest

pytestmark = pytest.mark.unit

CLAUDE_CAP = 10_000
PREVIEW = 2_000
ENFORCEMENT_IDS = [f"enf-{i}" for i in range(8)]


def _enforcements() -> str:
    return "\n\n".join(
        f"=== ENFORCEMENT [IMPORTANT] ===\nID: {i}\nRule {i} must reach the model.\n=====" for i in ENFORCEMENT_IDS
    )


def _write_memory(path, count=15):
    entries = [
        {"ts": "2026-10-15T17:10:00Z", "tool": "Bash", "error": "boom\n" + "e" * 200, "input": "i" * 150}
        for _ in range(count)
    ]
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))


@pytest.fixture
def claude_pretool(monkeypatch, tmp_path):
    from hooks import config, hook_manager, tool_memory
    from hooks.targets import emitter

    monkeypatch.setenv("AGENTIHOOKS_TARGET", "claude")
    monkeypatch.setattr(emitter, "_forced", True)
    monkeypatch.setattr(emitter, "_buffer", [])
    monkeypatch.setattr(config, "SECRETS_MODE", "off")
    for flag in ("QUOTA_POLICY_ENABLED", "QUOTA_USAGE_INJECTION_ENABLED", "BROADCAST_ENABLED", "RETRY_BREAKER_ENABLED"):
        monkeypatch.setattr(config, flag, False)
    monkeypatch.setattr(config, "BRAIN_ENABLED", True)
    monkeypatch.setattr(config, "ENFORCEMENT_INJECTION_ENABLED", True)
    monkeypatch.setattr("hooks.context.enforcement.increment_and_get_count", Mock(return_value=20))
    monkeypatch.setattr("hooks.context.context_recycle.directive", Mock(return_value=None))
    monkeypatch.setattr("hooks.lifecycle.guard.pretool", Mock(return_value=None))
    monkeypatch.setattr(
        "hooks.context.brain_adapter.maybe_refresh_on_tool_call",
        Mock(return_value="BRAIN CONTEXT\n" + "\n".join("* arc " + "a" * 90 for _ in range(80))),
    )
    monkeypatch.setattr("hooks.context.enforcement.get_pretool_enforcements", Mock(return_value=_enforcements()))
    monkeypatch.setattr(hook_manager, "_inbox_blocks", Mock(return_value=[]))
    monkeypatch.setattr(hook_manager, "_wait_nudge_blocks", Mock(return_value=[]))
    monkeypatch.setattr(hook_manager, "_refocus_blocks", Mock(return_value=["=== SWARM REFOCUS ===\n" + "r" * 1500]))
    memory = tmp_path / "memory.ndjson"
    _write_memory(memory)
    monkeypatch.setattr(tool_memory, "MEMORY_PATH", memory)
    monkeypatch.setattr(tool_memory, "_is_tool_seen", Mock(return_value=False))
    monkeypatch.setattr(tool_memory, "_mark_tool_seen", Mock())
    return hook_manager


def test_over_budget_tool_call_keeps_every_enforcement_in_the_preview(claude_pretool, capsys):
    claude_pretool.on_pre_tool_use({"session_id": "budget", "tool_name": "Unknown", "tool_input": {}})

    context = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert len(context.encode("utf-16-le")) // 2 < CLAUDE_CAP
    for enforcement_id in ENFORCEMENT_IDS:
        end = context.index(f"Rule {enforcement_id} must reach the model.")
        assert end < PREVIEW, f"{enforcement_id} lands at {end}, past the preview"
    assert "[context budget:" in context
    assert "tool memory" in context.splitlines()[-1]


def test_session_start_stdout_fits_the_budget_with_doctrine_first(monkeypatch, capsys):
    from hooks.common import inject_context
    from hooks.context.injection_budget import units
    from hooks.targets import emitter

    monkeypatch.setenv("AGENTIHOOKS_TARGET", "claude")
    emitter.force_buffer(plain=True)
    inject_context("BRAIN CONTEXT\n" + "b" * 7000, also_log=False, skip_compression=True, kind="brain")
    inject_context("TOOL MEMORY\n" + "t" * 4000, also_log=False, skip_compression=True, kind="tool_memory")
    inject_context(_enforcements(), also_log=False, skip_compression=True, kind="enforcement")
    emitter.flush("SessionStart")

    out = capsys.readouterr().out
    assert not out.lstrip().startswith("{"), "session start stays plain stdout"
    assert units(out) < CLAUDE_CAP
    assert out.index(f"Rule {ENFORCEMENT_IDS[-1]} must reach the model.") < PREVIEW
    assert "left out tool memory" in out


def test_under_budget_keeps_every_block_in_priority_order():
    from hooks.context.injection_budget import fit

    blocks = [("tool_memory", "memory"), ("brain", "brain"), ("enforcement", "rule"), ("alert", "operator")]
    assert fit(blocks, 9000) == "operator\nrule\nbrain\nmemory"


def test_unknown_kind_ranks_last():
    from hooks.context.injection_budget import fit

    blocks = [("tool_memory", "memory"), ("mystery", "note"), ("other", "notice"), ("brain", "brain")]
    assert fit(blocks, 9000) == "notice\nbrain\nmemory\nnote"


@pytest.mark.parametrize(
    ("severity", "kind"),
    [("nuclear", "alert"), ("critical", "alert"), ("alert", "brain"), ("warning", "brain"), ("info", "brain")],
)
def test_only_critical_and_nuclear_broadcasts_rank_as_alerts(severity, kind):
    from hooks.context.broadcast import context_kind, format_critical_context

    msg = {"id": "b1", "severity": severity, "message": "signal quoting [CRITICAL] and\n  - [NUCLEAR] text"}
    assert context_kind(format_critical_context([msg], "")) == kind


def test_claimed_inbox_block_is_never_shortened_or_dropped():
    from hooks.context.injection_budget import fit

    inbox = "=== INBOX: message m1 from operator ===\n" + "\n".join("order " + "o" * 90 for _ in range(120))
    small = "=== INBOX: message m2 from master ===\nshort"
    out = fit([("enforcement", "rule"), ("inbox", inbox), ("alert", "a" * 600), ("inbox", small)], 9000)
    assert out.startswith(inbox + "\n" + small + "\n")
    assert "left out alerts, enforcements" in out.splitlines()[-1]


def test_without_a_budget_blocks_keep_arrival_order():
    from hooks.context.injection_budget import fit

    assert fit([("tool_memory", "memory"), ("", "skipped"), ("enforcement", "rule")], None) == "memory\nskipped\nrule"
    assert fit([("brain", ""), ("alert", "a")], None) == "a"


def test_plain_context_is_dropped_from_a_block_message(monkeypatch):
    from hooks.targets import emitter

    monkeypatch.setenv("AGENTIHOOKS_TARGET", "claude")
    emitter.force_buffer(plain=True)
    emitter.buffer_context("session context", "enforcement")
    assert emitter.drain_for_block() == ""
    assert not emitter.has_buffered()
    emitter.force_buffer()
    emitter.buffer_context("tool context")
    assert emitter.drain_for_block() == "tool context"


def test_shortened_block_is_named_and_total_stays_under_budget():
    from hooks.context.injection_budget import fit, units

    out = fit([("enforcement", "rule"), ("brain", "\n".join("line " + "x" * 50 for _ in range(100)))], 2000)
    assert units(out) <= 2000
    assert out.startswith("rule\nline ")
    assert out.endswith("[context budget: shortened brain feed and broadcasts to stay under 2000 characters]")


def test_budget_counts_utf16_units():
    from hooks.context.injection_budget import fit, units

    out = fit([("alert", "\U0001f7e3" * 3000), ("brain", "b" * 600)], 5000)
    assert units(out) <= 5000
    assert "left out brain feed and broadcasts" in out


def test_budget_applies_only_to_claude(monkeypatch):
    from hooks import config
    from hooks.context.injection_budget import budget_chars

    assert budget_chars("claude") == config.HOOK_INJECTION_MAX_CHARS < CLAUDE_CAP
    assert budget_chars("codex") is None
    monkeypatch.setattr(config, "HOOK_INJECTION_MAX_CHARS", 0)
    assert budget_chars("claude") is None


def test_tool_memory_is_plain_short_and_few(monkeypatch, tmp_path):
    from hooks import tool_memory

    memory = tmp_path / "memory.ndjson"
    _write_memory(memory, count=20)
    monkeypatch.setattr(tool_memory, "MEMORY_PATH", memory)
    injected = Mock()
    monkeypatch.setattr("hooks.common.inject_context", injected)

    tool_memory.inject_memory()

    text = injected.call_args.args[0]
    assert injected.call_args.kwargs["kind"] == "tool_memory"
    assert not set(text) & set("╔║╠╚═")
    entries = text.splitlines()[1:]
    assert len(entries) == tool_memory.MAX_SHOW <= 5
    assert all(len(line) == tool_memory.ENTRY_CHARS and line.startswith("- [") for line in entries)
