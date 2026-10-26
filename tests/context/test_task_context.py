import json
from pathlib import Path
from unittest.mock import patch

import pytest

from hooks.context import brain_adapter, task_context
from hooks.targets import emitter
from hooks.targets.capabilities import can_inject_context

FIXTURE = Path(__file__).parents[1] / "fixtures/swarm_v2/task-context-hooks.json"
INPUTS = json.loads(FIXTURE.read_text(encoding="utf-8"))
PACKS = INPUTS["packs"]


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setattr("hooks.config.AGENTIHOOKS_HOME", tmp_path)
    monkeypatch.setattr("hooks.config.BRAIN_ENABLED", True)
    monkeypatch.setattr("hooks.config.BRAIN_REFRESH_TOOL_CALLS", 1)
    monkeypatch.delenv("AGENTIHOOKS_TASK_CONTEXT_OFF", raising=False)
    emitter.drain()
    yield tmp_path
    emitter.drain()


def _target(monkeypatch, target):
    monkeypatch.setenv("AGENTIHOOKS_TARGET", target)


def _emitted(capsys, event):
    emitter.flush(event)
    out = capsys.readouterr().out
    if not out.strip().startswith("{"):
        return out
    return json.loads(out).get("hookSpecificOutput", {}).get("additionalContext", "")


def _start(payload, pack):
    with (
        patch("hooks.context.brain_adapter.force_refresh", return_value=False),
        patch("hooks.context.project_cache.project_context", return_value=pack),
        patch("hooks.context.project_identity.resolve_project", return_value=None),
        patch("hooks.context.project_sessions.record_session"),
    ):
        brain_adapter.inject_on_session_start(payload["session_id"], payload["cwd"])


def _tool_call(payload, pack):
    with (
        patch("hooks.context.brain_adapter._refresh", return_value=None),
        patch("hooks.context.project_cache.project_context", return_value=pack) as retrieval,
    ):
        returned = brain_adapter.maybe_refresh_on_tool_call(
            payload["session_id"], 1, claim_delivery=can_inject_context("PreToolUse")
        )
    retrieval.assert_called_once_with(payload["session_id"])
    return returned


@pytest.mark.parametrize("target", ["claude", "codex"])
def test_a_each_target_receives_the_scoped_pack_with_a_receipt(home, monkeypatch, capsys, target):
    _target(monkeypatch, target)
    hooks = INPUTS["hooks"][target]
    session = hooks["SessionStart"]["session_id"]
    _start(hooks["SessionStart"], PACKS["first"])
    assert PACKS["first"] in _emitted(capsys, "SessionStart")
    receipt = task_context.receipt(session)
    assert receipt["event"] == "SessionStart"
    assert receipt["target"] == target
    assert receipt["digest"] == task_context.digest(PACKS["first"])

    delivered = _tool_call(hooks["PreToolUse"], PACKS["second"])
    if target == "claude":
        assert delivered == PACKS["second"]
    else:
        assert delivered is None
        assert task_context.deliver_pending(session, "PostToolUse") == PACKS["second"]
    latest = task_context.receipt(session)
    assert latest["digest"] == task_context.digest(PACKS["second"])
    assert latest["event"] == ("PreToolUse" if target == "claude" else "PostToolUse")
    assert latest["at"] >= receipt["at"] > 0
    assert task_context.receipt("another session") == {}
    assert task_context.outcomes()[target]["delivered"] == 2
    assert Path(f"{task_context._outcomes_path()}.lock").exists()


def test_a_second_independent_fixture_does_not_lean_on_the_first(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr("hooks.config.BRAIN_ENABLED", True)
    monkeypatch.delenv("AGENTIHOOKS_TASK_CONTEXT_OFF", raising=False)
    emitter.drain()
    records = []
    for name in ("one", "two"):
        monkeypatch.setattr("hooks.config.AGENTIHOOKS_HOME", tmp_path / name)
        _target(monkeypatch, "codex")
        payload = INPUTS["hooks"]["codex"]["SessionStart"]
        _start(payload, PACKS["first"])
        assert PACKS["first"] in _emitted(capsys, "SessionStart")
        records.append((task_context.receipt(payload["session_id"])["digest"], task_context.outcomes()))
    assert records[0] == records[1]
    assert records[0][1] == {"codex": {"delivered": 1}}


def test_b_codex_pre_tool_retrieval_is_not_counted_as_delivery(home, monkeypatch, capsys):
    _target(monkeypatch, "codex")
    hooks = INPUTS["hooks"]["codex"]
    session = hooks["SessionStart"]["session_id"]
    _start(hooks["SessionStart"], PACKS["first"])
    _emitted(capsys, "SessionStart")
    before = task_context.receipt(session)

    assert _tool_call(hooks["PreToolUse"], PACKS["second"]) is None
    assert task_context.offer(session, "PreToolUse", PACKS["second"]) is None
    assert task_context.receipt(session) == before
    assert task_context.outcomes()["codex"] == {"delivered": 1, "unsupported_event": 2}

    emitter.buffer_context(PACKS["second"], "brain")
    assert _emitted(capsys, "PreToolUse") == ""
    assert task_context.receipt(session) == before


def test_b_a_disabled_target_gets_no_pack_and_keeps_its_fleet_feed(home, monkeypatch):
    _target(monkeypatch, "codex")
    monkeypatch.setenv("AGENTIHOOKS_TASK_CONTEXT_OFF", "claude, codex")
    session = INPUTS["hooks"]["codex"]["SessionStart"]["session_id"]
    with (
        patch("hooks.context.brain_adapter._refresh", return_value={"created_ids": ["fleet"]}),
        patch("hooks.context.project_cache.project_context", return_value=PACKS["first"]),
        patch("hooks.context.broadcast.get_broadcast_context", return_value="fleet feed") as fleet,
    ):
        assert brain_adapter.maybe_refresh_on_tool_call(session, 1) == "fleet feed"
    fleet.assert_called_once_with(session, ["fleet"])
    assert task_context.deliver_pending(session, "PostToolUse") is None
    assert task_context.receipt(session) == {}
    assert task_context.outcomes() == {"codex": {"disabled": 1}}
    monkeypatch.setenv("AGENTIHOOKS_TASK_CONTEXT_OFF", "claude")
    assert task_context.offer(session, "PostToolUse", PACKS["first"]) == PACKS["first"]
    monkeypatch.setenv("AGENTIHOOKS_TASK_CONTEXT_OFF", "codex,claude")
    assert task_context.offer(session, "PostToolUse", PACKS["second"]) is None


@pytest.mark.parametrize("target", ["claude", "codex"])
def test_c_unchanged_packs_are_not_repeated_and_compaction_gets_the_current_pack(home, monkeypatch, capsys, target):
    _target(monkeypatch, target)
    hooks = INPUTS["hooks"][target]
    session = hooks["SessionStart"]["session_id"]
    _start(hooks["SessionStart"], PACKS["first"])
    _emitted(capsys, "SessionStart")
    for _ in range(3):
        assert _tool_call(hooks["PreToolUse"], PACKS["first"]) is None
        assert task_context.deliver_pending(session, "PostToolUse") is None
    task_context.hold(session, PACKS["second"])

    _start(hooks["Compact"], PACKS["first"])
    assert PACKS["first"] in _emitted(capsys, "SessionStart")
    assert task_context.deliver_pending(session, "PostToolUse") is None
    assert task_context.receipt(session)["digest"] == task_context.digest(PACKS["first"])
    counts = task_context.outcomes()[target]
    assert counts["delivered"] == 2
    assert counts["deduplicated"] == 3


def test_c_a_newer_held_pack_replaces_an_older_one(home, monkeypatch):
    _target(monkeypatch, "codex")
    task_context.hold("held", PACKS["first"])
    task_context.hold("held", PACKS["second"])
    assert task_context.deliver_pending("held", "PostToolUse") == PACKS["second"]
    assert task_context.deliver_pending("held", "PostToolUse") is None
    task_context.hold("held", PACKS["first"])
    task_context.restart("held")
    assert task_context.receipt("held") == {}
    assert task_context.deliver_pending("held", "PostToolUse") is None
    assert task_context.offer("held", "PostToolUse", "") is None

    task_context.hold("fresh", PACKS["first"])
    assert task_context.offer("fresh", "PostToolUse", PACKS["second"]) == PACKS["second"]
    assert task_context.deliver_pending("fresh", "PostToolUse") is None

    task_context.hold("pre", PACKS["first"])
    assert task_context.deliver_pending("pre", "PreToolUse") is None
    assert task_context.receipt("pre") == {}


def test_c_a_repeated_current_pack_drops_an_older_held_pack(home, monkeypatch):
    _target(monkeypatch, "codex")
    assert task_context.offer("back", "PostToolUse", PACKS["first"]) == PACKS["first"]
    assert task_context.offer("back", "PreToolUse", PACKS["second"]) is None
    assert task_context.offer("back", "PreToolUse", PACKS["first"]) is None
    assert task_context.deliver_pending("back", "PostToolUse") is None
    assert task_context.receipt("back")["digest"] == task_context.digest(PACKS["first"])
    assert task_context.outcomes()["codex"] == {"delivered": 1, "unsupported_event": 1, "deduplicated": 1}


def test_codex_post_tool_use_delivers_the_held_pack(home, monkeypatch):
    import hooks.common as common
    import hooks.context.broadcast as broadcast
    import hooks.context.enforcement as enforcement
    from hooks import hook_manager

    _target(monkeypatch, "codex")
    payload = INPUTS["hooks"]["codex"]["PostToolUse"]
    captured = []
    monkeypatch.setattr(common, "inject_context", lambda message, **kwargs: captured.append(message))
    monkeypatch.setattr(broadcast, "get_posttool_context", lambda session_id: None)
    monkeypatch.setattr(enforcement, "get_posttool_enforcements", lambda session_id, cwd, **kw: None)
    task_context.hold(payload["session_id"], PACKS["second"])
    hook_manager.on_post_tool_use(payload)
    assert PACKS["second"] in captured
    assert task_context.receipt(payload["session_id"])["event"] == "PostToolUse"


def test_status_reports_delivery_outcomes(home, monkeypatch):
    _target(monkeypatch, "claude")
    task_context.offer("status", "PreToolUse", PACKS["first"])
    with patch("hooks.context.brain_adapter._get_source", return_value=None):
        status = brain_adapter.get_status()
    assert status[task_context.OUTCOMES] == {"claude": {"delivered": 1}}
