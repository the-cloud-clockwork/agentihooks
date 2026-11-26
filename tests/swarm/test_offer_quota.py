from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from scripts.events.envelope import Envelope
from scripts.swarm import capacity, offer_quota, profile_choice, runtime
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
    monkeypatch.setattr(runtime.prompt, "build", lambda *_args, **_kwargs: "Plan the task")
    monkeypatch.setattr(
        local,
        "_profile_decision",
        Mock(return_value=profile_choice.ProfileDecision("planner", "task", "required profile")),
    )

    def launch(*args: Any, **kwargs: Any) -> runtime.Placed:
        return launcher(*args, **kwargs)

    monkeypatch.setattr(local, "_launch", launch)
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


@pytest.fixture
def config(monkeypatch: pytest.MonkeyPatch) -> SwarmConfig:
    monkeypatch.setattr(offer_quota.plugins, "claude_only", lambda profile: profile == "claude-profile")
    return SwarmConfig("example", ".", 1, 1, lanes={"plan": {"agent": "auto", "profile": "planner"}})


def account(harness: str, sessions: int = 0, cap: int = 2, name: str = "worker") -> capacity.Account:
    return capacity.Account(harness, name, "OPEN", sessions, 50, 50, cap)


def event(descriptor: dict[str, Any] | None = None) -> Envelope:
    data = dict[str, Any]({"lane": "plan", "generation": 4})
    if descriptor is not None:
        data["quota"] = descriptor
    return Envelope("offer", "task.ready", "example", "task", "task", 4, 100, "hub", data)


@pytest.mark.feature("event-offer-account-seats")
def test_restrictions_preserve_only_admission_fields() -> None:
    task = dict[str, Any](
        {
            "requires_harness": "codex",
            "profile": "planner",
            "avoid_harness": "claude",
            "description": "private task text",
            "launch_assignment": {"harness": "codex", "account": "worker", "profile": "saved", "model": "model"},
            "handoff_envelope": {
                "reason": "quota",
                "launch": {"harness": "claude", "account": "previous", "profile": "old", "model": "unused"},
                "body": "private handoff text",
            },
        }
    )
    result = offer_quota.restrictions(task)
    assert result == {
        "requires_harness": "codex",
        "profile": "planner",
        "avoid_harness": "claude",
        "launch_assignment": {"harness": "codex", "account": "worker", "profile": "saved"},
        "handoff_envelope": {
            "reason": "quota",
            "launch": {"harness": "claude", "account": "previous", "profile": "old"},
        },
    }
    result["launch_assignment"]["profile"] = "changed"
    result["handoff_envelope"]["launch"]["profile"] = "changed"
    assert task["launch_assignment"]["profile"] == "saved"
    assert task["handoff_envelope"]["launch"]["profile"] == "old"
    assert offer_quota.restrictions({}) == {}
    assert offer_quota.restrictions({"launch_assignment": {}, "handoff_envelope": {}}) == {}
    assert offer_quota.restrictions({"handoff_envelope": {"body": "text"}}) == {"handoff_envelope": {"launch": {}}}


@pytest.mark.parametrize("agent", [None, "", "auto"])
@pytest.mark.feature("event-offer-account-seats")
def test_permitted_automatic_lanes(config: SwarmConfig, agent: str | None) -> None:
    selected = replace(config, lanes={"plan": {"agent": agent}})
    assert offer_quota.permitted(selected, "plan", {}) == ("claude", "codex")


@pytest.mark.parametrize(
    ("agent", "task", "expected"),
    [
        ("auto", {"requires_harness": "claude"}, ("claude",)),
        ("codex", {}, ("codex",)),
        ("claude", {"requires_harness": "codex"}, ()),
        ("codex", {"profile": "claude-profile"}, ()),
        ("auto", {"profile": "claude-profile"}, ("claude",)),
        ("auto", {"requires_harness": "other"}, ()),
        ("other", {}, ()),
        ("auto", {"launch_assignment": {"profile": "claude-profile"}, "profile": "planner"}, ("claude",)),
        ("auto", {"handoff_envelope": {"launch": {"profile": "claude-profile"}}}, ("claude",)),
        (
            "auto",
            {
                "launch_assignment": {"profile": "planner"},
                "handoff_envelope": {"launch": {"profile": "claude-profile"}},
            },
            ("claude", "codex"),
        ),
    ],
)
@pytest.mark.feature("event-offer-account-seats")
def test_permitted_preserves_constraints(
    config: SwarmConfig, agent: str, task: dict[str, Any], expected: tuple[str, ...]
) -> None:
    selected = replace(config, lanes={"plan": {"agent": agent, "profile": "planner"}})
    assert offer_quota.permitted(selected, "plan", task) == expected


@pytest.mark.feature("event-offer-account-seats")
def test_permitted_uses_lane_profile(config: SwarmConfig) -> None:
    selected = replace(config, lanes={"plan": {"agent": "auto", "profile": "claude-profile"}})
    assert offer_quota.permitted(selected, "plan", {}) == ("claude",)


@pytest.mark.feature("event-offer-account-seats")
def test_available_selects_free_permitted_harnesses(config: SwarmConfig) -> None:
    rows = [account("claude", sessions=2), account("codex")]
    assert offer_quota.available(config, "plan", {}, rows, {}) == ("codex",)
    assert offer_quota.available(config, "plan", {"requires_harness": "claude"}, rows, {}) == ()
    assert (
        offer_quota.available(config, "plan", {}, [account("claude", sessions=2), account("codex", sessions=2)], {})
        == ()
    )
    assert offer_quota.available(config, "plan", {}, [], {}) == ()


@pytest.mark.feature("event-offer-account-seats")
def test_available_reuses_quota_thresholds(config: SwarmConfig) -> None:
    rows = [replace(account("claude"), week_left=15), account("codex")]
    assert offer_quota.available(config, "plan", {}, rows, {}) == ("claude", "codex")
    assert offer_quota.available(config, "plan", {}, rows, {"AGENTIHOOKS_HANDOFF_WARN_WEEK_PCT": "80"}) == ("codex",)
    assert offer_quota.available(config, "plan", {}, [replace(account("codex"), state="UNKNOWN")], {}) == ()
    assert offer_quota.available(config, "plan", {}, [replace(account("claude"), five_left=None)], {}) == ("claude",)


@pytest.mark.feature("event-offer-account-seats")
def test_available_excludes_quota_predecessor(config: SwarmConfig) -> None:
    task = dict[str, Any](
        {"handoff_envelope": {"reason": "quota", "launch": {"harness": "claude", "account": "worker"}}}
    )
    rows = [account("claude"), account("codex")]
    assert offer_quota.available(config, "plan", task, rows, {}) == ("codex",)
    task["handoff_envelope"]["reason"] = "context"
    assert offer_quota.available(config, "plan", task, rows, {}) == ("claude", "codex")
    task["handoff_envelope"]["reason"] = "quota"
    task["launch_assignment"] = {"harness": "codex", "account": "worker"}
    assert offer_quota.available(config, "plan", task, rows, {}) == ("claude",)


@pytest.mark.feature("event-offer-account-seats")
def test_available_master_unreserves_capacity(config: SwarmConfig) -> None:
    row = replace(account("claude", cap=0), reserved="primary", unreserved_cap=2)
    assert offer_quota.available(config, "plan", {}, [row], {}) == ()
    assert offer_quota.available(config, "master", {}, [row], {}) == ("claude",)
    assert row.cap == 0
    assert row.reserved == "primary"


@pytest.mark.feature("event-offer-account-seats")
def test_fit_preserves_placement_before_quota(config: SwarmConfig) -> None:
    read = Mock(return_value=[account("codex")])
    placement = Mock(return_value=False)
    fits = offer_quota.Fit(config, "plan", placement, read)
    offered = event({})
    assert not fits(offered)
    assert not fits.held
    placement.assert_called_once_with(offered)
    read.assert_not_called()


@pytest.mark.feature("event-offer-account-seats")
def test_fit_holds_legacy_offers_without_reading_accounts(config: SwarmConfig) -> None:
    read = Mock(return_value=[account("codex")])
    fits = offer_quota.Fit(config, "plan", lambda offered: True, read)
    assert not fits(event())
    assert fits.held
    read.assert_not_called()


@pytest.mark.feature("event-offer-account-seats")
def test_fit_reads_once_and_preserves_hold_until_poll_ends(
    config: SwarmConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OFFER_QUOTA_TEST", "current")
    monkeypatch.setattr(offer_quota.time, "time", lambda: 12345.0)
    read = Mock(return_value=[account("claude", sessions=2), account("codex")])
    placement = Mock(return_value=True)
    fits = offer_quota.Fit(config, "plan", placement, read)
    blocked = event({"requires_harness": "claude"})
    original = blocked.to_dict()
    assert not fits(blocked)
    assert fits.held
    assert fits(event({}))
    assert fits.held
    assert blocked.to_dict() == original
    assert read.call_count == 1
    assert read.call_args.args[0]["OFFER_QUOTA_TEST"] == "current"
    assert read.call_args.args[1] == 12345.0
    assert read.call_args.kwargs == {"refresh": False}


@pytest.mark.feature("event-offer-account-seats")
def test_fit_empty_read_is_cached_until_next_poll(config: SwarmConfig) -> None:
    read = Mock(side_effect=[[], [account("claude")]])
    first = offer_quota.Fit(config, "plan", lambda offered: True, read)
    assert not first(event({}))
    assert not first(event({}))
    assert read.call_count == 1
    second = offer_quota.Fit(config, "plan", lambda offered: True, read)
    assert second(event({}))
    assert not second.held
    assert read.call_count == 2


@pytest.mark.feature("event-offer-account-seats")
def test_fit_default_reader_is_lazy(config: SwarmConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    read = Mock(return_value=[account("codex")])
    monkeypatch.setattr(offer_quota.capacity, "accounts", read)
    fits = offer_quota.Fit(config, "plan", lambda offered: True)
    read.assert_not_called()
    assert fits(event({"requires_harness": "codex"}))
    assert not fits.held
    assert read.call_count == 1
