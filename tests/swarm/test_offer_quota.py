from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts.swarm import capacity, profile_choice, runtime
from scripts.swarm.store import SwarmConfig
from scripts.swarm.tick import SpawnError


def _event_task(requires_harness: str = "") -> dict[str, object]:
    return {
        "id": "task-a",
        "title": "Plan the task",
        "profile": "planner",
        "requires_harness": requires_harness,
        "execution_id": "admitted-event",
        "admitted_execution_id": "admitted-event",
        "execution_generation": 7,
        "claim_generation": 3,
    }


def _event_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rows: list[capacity.Account]
) -> tuple[runtime.HerdrRuntime, SwarmConfig, Mock, Mock]:
    local = runtime.HerdrRuntime(home=tmp_path, choose=lambda *_args: ("claude", "priority"))
    reader = Mock(side_effect=lambda *_args, **_kwargs: list(rows))
    launcher = Mock(return_value=runtime.Placed("pane-a", "claude", "claude-a"))
    monkeypatch.setenv("EVENT_ACCOUNT_READING", "present")
    monkeypatch.setattr(capacity, "accounts", reader)
    monkeypatch.setattr(runtime.plugins, "claude_only", lambda _profile: False)
    monkeypatch.setattr(runtime.time, "time", lambda: 1_234_567_890.0)
    monkeypatch.setattr(runtime.prompt, "build", Mock(return_value="Plan the task"))
    monkeypatch.setattr(
        local,
        "_profile_decision",
        Mock(return_value=profile_choice.ProfileDecision("planner", "task", "required profile")),
    )
    monkeypatch.setattr(local, "_launch", launcher)
    config = SwarmConfig("rig", str(tmp_path), 1, 0, code="a1b2c3", scaling="manual")
    return local, config, reader, launcher


@pytest.mark.feature("event-offer-account-seats")
@pytest.mark.parametrize("codex_sessions", [0, 6])
def test_a_fresh_admitted_runtime_reads_accounts_before_launching_required_claude(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, codex_sessions: int
) -> None:
    rows = [
        capacity.Account("claude", "claude-a", "OPEN", 3, None, 90, 3),
        capacity.Account("codex", "codex-a", "OPEN", codex_sessions, None, 90, 6),
    ]
    local, config, reader, launcher = _event_runtime(tmp_path, monkeypatch, rows)

    with pytest.raises(SpawnError, match="(?i)quota|seat|account"):
        local.spawn(config, "plan", "planner@a1b2c3-0001", _event_task("claude"))

    launcher.assert_not_called()
    reader.assert_called_once()
    assert reader.call_args.args[0]["EVENT_ACCOUNT_READING"] == "present"
    assert reader.call_args.args[1] == 1_234_567_890.0
    assert reader.call_args.kwargs["refresh"] is False


@pytest.mark.feature("event-offer-account-seats")
def test_a_mixed_admitted_offer_uses_codex_when_only_codex_has_an_account_seat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [
        capacity.Account("claude", "claude-a", "OPEN", 3, None, 90, 3),
        capacity.Account("codex", "codex-a", "OPEN", 0, None, 90, 6),
    ]
    local, config, reader, launcher = _event_runtime(tmp_path, monkeypatch, rows)

    placed = local.spawn(config, "plan", "planner@a1b2c3-0001", _event_task())

    reader.assert_called_once()
    launcher.assert_called_once()
    argv = launcher.call_args.args[4]
    assert argv[argv.index("--agent") + 1] == "codex"
    assert argv[argv.index("--route") + 1] == "codex-a"
    assert argv[argv.index("--profile") + 1] == "planner"
    assert placed.profile_decision["profile"] == "planner"
    assert [(row.name, row.sessions) for row in local._quota_accounts] == [("claude-a", 3), ("codex-a", 1)]


@pytest.mark.feature("event-offer-account-seats")
def test_an_admitted_spawn_keeps_quota_rows_owned_by_the_tick_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [capacity.Account("claude", "claude-a", "OPEN", 0, None, 90, 3)]
    local, config, reader, launcher = _event_runtime(tmp_path, monkeypatch, rows)
    local._quota_accounts = list(rows)
    local._quota_spawn_owned = False
    reader.side_effect = AssertionError("tick pass already supplied quota rows")

    local.spawn(config, "plan", "planner@a1b2c3-0001", _event_task("claude"))

    reader.assert_not_called()
    launcher.assert_called_once()
    assert [(row.name, row.sessions) for row in local._quota_accounts] == [("claude-a", 1)]
