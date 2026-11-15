"""The shared tick helpers live in the common step module and the swarm tick module re-exports each one."""

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.swarm import tick
from scripts.tick.steps import common

pytestmark = pytest.mark.unit

MOVED = (
    "skip_refused",
    "skip_dropped",
    "_operator_line",
    "_drop",
    "_follow",
    "_rebind",
    "_collected",
    "_still_held",
    "_ended",
    "_current_owner",
    "_reopen",
)


@pytest.mark.feature("swarm-tick-common-helpers")
@pytest.mark.parametrize("name", MOVED)
def test_tick_reexports_the_common_helper(name: str) -> None:
    helper = getattr(common, name)
    assert helper.__module__ == "scripts.tick.steps.common"
    assert getattr(tick, name) is helper


@pytest.mark.feature("swarm-tick-common-helpers")
def test_common_helpers_reach_tick_seams_through_the_tick_module(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[object, ...]] = []
    monkeypatch.setattr(tick, "_rebind", lambda *args: seen.append(args))
    agent = SimpleNamespace(name="engineer@1-1")
    assert common._follow("slug", "store", "runtime", agent, 7) == (
        None,
        ["held engineer@1-1 until one pane holds its resumed process 7"],
    )
    assert seen == [("slug", "store", "runtime", agent, 7)]


@dataclass(frozen=True)
class _Agent:
    """Agent record with the fields the rebind helper reads and replaces."""

    name: str
    pane_id: str
    conversation_id: str
    profile_decision: dict[str, Any]


class _Ledger:
    """Ledger fake that records task updates and answers with a fixed live row."""

    def __init__(self, live: dict[str, object]) -> None:
        self.live = live
        self.calls: list[tuple[object, ...]] = []

    def update_task(self, slug: str, task_id: str, fields: dict[str, object], **guard: object) -> dict[str, object]:
        self.calls.append((slug, task_id, fields, guard))
        return self.live


@pytest.mark.feature("swarm-tick-common-helpers")
def test_still_held_needs_active_state_owner_and_generation() -> None:
    expected = {"claimed_by": "a", "claim_generation": 2}
    assert common._still_held({"state": "claimed", "claimed_by": "a", "claim_generation": 2}, expected) is True
    assert common._still_held({"state": "pr", "claimed_by": "a", "claim_generation": 2}, expected) is True
    assert common._still_held({"state": "open", "claimed_by": "a", "claim_generation": 2}, expected) is False
    assert common._still_held({"state": "claimed", "claimed_by": "b", "claim_generation": 2}, expected) is False
    assert common._still_held({"state": "claimed", "claimed_by": "a", "claim_generation": 3}, expected) is False
    assert (
        common._still_held({"state": "claimed", "claimed_by": "a"}, {"claimed_by": "a", "claim_generation": 0}) is True
    )
    assert common._still_held(None, expected) is False


@pytest.mark.feature("swarm-tick-common-helpers")
def test_current_owner_accepts_an_unclaimed_row_or_its_own_claim() -> None:
    agent = SimpleNamespace(name="a", runtime_backend="local", runtime_target={}, hive="")
    assert common._current_owner(agent, {}) is True
    assert common._current_owner(agent, {"claimed_by": ""}) is True
    assert common._current_owner(agent, {"claimed_by": "a"}) is True
    assert common._current_owner(agent, {"claimed_by": "b"}) is False


@pytest.mark.feature("swarm-reopened-claim-loss")
@pytest.mark.parametrize(
    "placed",
    [
        {"runtime_backend": "kubernetes", "runtime_target": {}, "hive": ""},
        {"runtime_backend": "local", "runtime_target": {"host_id": "swarm-hive-003"}, "hive": ""},
        {"runtime_backend": "local", "runtime_target": {}, "hive": "swarm-hive-003"},
    ],
)
def test_a_hive_agent_loses_an_emptied_claim(monkeypatch: pytest.MonkeyPatch, placed: dict[str, object]) -> None:
    monkeypatch.setenv("SWARM_HIVE_ID", "workstation")
    agent = SimpleNamespace(name="a", **placed)
    assert common._current_owner(agent, {"state": "open", "claimed_by": ""}) is False
    assert common._current_owner(agent, {"state": "open"}) is False
    assert common._current_owner(agent, {"claimed_by": "a"}) is True
    assert common._current_owner(agent, {}) is True


def test_an_agent_of_this_hive_keeps_an_emptied_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SWARM_HIVE_ID", "workstation")
    agent = SimpleNamespace(
        name="a",
        lane="eng",
        state="working",
        task="t1",
        runtime_backend="local",
        runtime_target={"host_id": "workstation"},
        hive="workstation",
    )
    assert common._current_owner(agent, {"state": "open", "claimed_by": ""}) is True
    assert common._ended(agent, {"t1": {"state": "claimed", "claimed_by": "b"}}) is False


@pytest.mark.feature("swarm-tick-common-helpers")
@pytest.mark.parametrize(
    ("lane", "state", "row", "ended"),
    [
        ("eng", "working", {}, False),
        ("eng", "finished", {}, True),
        ("eng", "working", {"done": True}, True),
        ("eng", "working", {"state": "done"}, True),
        ("eng", "working", {"state": "blocked"}, True),
        ("eng", "working", {"state": "handoff"}, True),
        ("eng", "working", {"state": "claimed"}, False),
        ("eng", "working", {"awaiting": "verify"}, True),
        ("eng", "working", {"awaiting": "verify", "claimed_by": "a"}, False),
        ("eng", "working", {"awaiting": "review"}, False),
        ("master", "working", {"state": "done"}, False),
        ("master", "finished", {"state": "done"}, True),
    ],
)
def test_ended_reads_the_task_row_except_for_masters(
    lane: str, state: str, row: dict[str, object], ended: bool
) -> None:
    agent = SimpleNamespace(
        name="a", lane=lane, state=state, task="t1", runtime_backend="local", runtime_target={}, hive=""
    )
    assert common._ended(agent, {"t1": row}) is ended


@pytest.mark.feature("swarm-tick-common-helpers")
def test_operator_line_skips_operator_control_notices() -> None:
    control = tick.control_notifications.CONTROL_REF
    line = SimpleNamespace(sender="operator", fyi=False, ref=control)
    assert common._operator_line(line) is True
    assert common._operator_line(SimpleNamespace(sender="operator", fyi=True, ref=f"{control}x")) is False
    assert common._operator_line(SimpleNamespace(sender="operator", fyi=True, ref="other")) is True
    assert common._operator_line(SimpleNamespace(sender="swarm", fyi=False, ref="other")) is False


@pytest.mark.feature("swarm-tick-common-helpers")
@pytest.mark.parametrize("expected", [None, {"claimed_by": "a", "claim_generation": 1}])
def test_reopen_guards_on_active_state_and_reports_the_live_state(expected: dict[str, object] | None) -> None:
    ledger = _Ledger({"state": "open", "claimed_by": ""})
    rows: dict[str, dict[str, object]] = {"t1": {"state": "claimed", "claimed_by": "a"}}
    assert common._reopen("slug", ledger, rows, "t1", expected) is True
    guard: dict[str, object] = {"if_state": tick.ACTIVE}
    if expected is not None:
        guard["if_match"] = expected
    assert ledger.calls == [("slug", "t1", {"state": "open", "claimed_by": ""}, guard)]
    assert rows["t1"] == {"state": "open", "claimed_by": ""}
    claimed: dict[str, object] = {"state": "pr", "claimed_by": "a"}
    ledger.live = claimed
    assert common._reopen("slug", ledger, rows, "t1") is False


@pytest.mark.feature("swarm-tick-common-helpers")
def test_rebind_takes_the_one_pane_holding_the_conversation() -> None:
    stored: list[tuple[str, object]] = []
    store = SimpleNamespace(put_agent=lambda slug, agent: stored.append((slug, agent)))
    agent = _Agent(name="a", pane_id="old", conversation_id="c1", profile_decision={"validation": {"state": "ok"}})
    runtime = SimpleNamespace(conversations=lambda: {"w1:p1": "c1", "w1:p2": "c2", "remote/p3": "c1"})
    rebound = common._rebind("slug", store, runtime, agent, 9)
    assert rebound.pane_id == "w1:p1"
    assert rebound.profile_decision == {"validation": {"state": "ok", "pid": 9}}
    assert stored == [("slug", rebound)]
    two = SimpleNamespace(conversations=lambda: {"w1:p1": "c1", "w1:p2": "c1"})
    assert common._rebind("slug", store, two, agent, 9) is None
    assert common._rebind("slug", store, SimpleNamespace(conversations=lambda: None), agent, 9) is None
    assert len(stored) == 1


@pytest.mark.feature("swarm-tick-common-helpers")
def test_follow_keeps_an_agent_without_a_pid_and_reports_a_rebind(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = SimpleNamespace(name="a")
    assert common._follow("slug", "store", "runtime", agent, None) == (agent, [])
    rebound = SimpleNamespace(pane_id="w1:p1")
    monkeypatch.setattr(tick, "_rebind", lambda *args: rebound)
    assert common._follow("slug", "store", "runtime", agent, 7) == (
        rebound,
        ["rebound a to its resumed process 7 in pane w1:p1"],
    )


@pytest.mark.feature("swarm-tick-common-helpers")
def test_skip_refused_turns_a_refusal_or_redis_timeout_into_a_skip_line(capsys: pytest.CaptureFixture[str]) -> None:
    from redis.exceptions import TimeoutError as RedisTimeoutError

    def refused() -> None:
        raise tick.LedgerRefused("no")

    def slow() -> None:
        raise RedisTimeoutError("slow")

    name = f"{__name__}.{refused.__qualname__}"
    assert common.skip_refused(lambda value: [value], "x") == ["x"]
    assert common.skip_refused(refused) == [f"skipped {name}: the ledger refused its write"]
    slow_name = f"{__name__}.{slow.__qualname__}"
    assert common.skip_refused(slow) == [f"skipped {slow_name}: its Redis read timed out"]
    err = capsys.readouterr().err
    assert f"{name} skipped, the ledger refused its write: " in err
    assert f"{slow_name} skipped, its Redis read timed out: slow" in err


@pytest.mark.feature("swarm-tick-common-helpers")
def test_skip_dropped_retries_once_then_skips(capsys: pytest.CaptureFixture[str]) -> None:
    from redis.exceptions import ConnectionError as RedisConnectionError

    calls: list[tuple[str, ...]] = []

    def flaky(*args: str) -> list[str]:
        calls.append(args)
        if len(calls) == 1:
            raise RedisConnectionError("gone")
        return ["ok", *args]

    def dead() -> None:
        raise RedisConnectionError("down")

    assert common.skip_dropped(flaky, "a", "b") == ["ok", "a", "b"]
    assert calls == [("a", "b"), ("a", "b")]
    assert common.skip_dropped(lambda: None) == []
    name = f"{__name__}.{dead.__qualname__}"
    assert common.skip_dropped(dead) == [f"skipped {name}: its Redis connection dropped or timed out twice"]
    assert f"{name} skipped, its Redis connection dropped or timed out twice: down" in capsys.readouterr().err


class _DropStore:
    """Claim store fake that records every call the drop helper makes."""

    def __init__(self, claims: int) -> None:
        self.redis = "redis"
        self.held_claims = claims
        self.calls: list[tuple[object, ...]] = []

    def claims(self, slug: str, task: str) -> int:
        self.calls.append(("claims", slug, task))
        return self.held_claims

    def claim_generation(self, slug: str, task: str) -> int:
        self.calls.append(("claim_generation", slug, task))
        return 5

    def refund_held_claim(self, slug: str, task: str, name: str, generation: int) -> None:
        self.calls.append(("refund", slug, task, name, generation))

    def release(self, slug: str, task: str, name: str) -> None:
        self.calls.append(("release", slug, task, name))

    def drop_agent(self, slug: str, name: str) -> None:
        self.calls.append(("drop_agent", slug, name))

    def keep_seat(self, slug: str, task: str, seat: str) -> None:
        self.calls.append(("keep_seat", slug, task, seat))


class _DropLedger(_Ledger):
    """Ledger fake that also answers the live task read with a fixed row."""

    def __init__(self, live: dict[str, object], row: dict[str, object]) -> None:
        super().__init__(live)
        self.row = row

    def task(self, slug: str, task_id: str) -> dict[str, object]:
        self.calls.append(("task", slug, task_id))
        return self.row


@pytest.mark.feature("swarm-tick-common-helpers")
@pytest.mark.parametrize(
    ("row", "refund", "claims", "live_row", "refunded", "reopened"),
    [
        ({"state": "claimed", "claimed_by": "a", "claim_generation": 2}, True, 1, None, True, True),
        ({"state": "claimed", "claimed_by": "a", "claim_generation": 2}, False, 1, None, False, True),
        ({"state": "claimed", "claimed_by": "a", "claim_generation": 2}, True, 0, None, False, True),
        ({"state": "claimed", "claimed_by": "a", "claim_generation": 2}, True, 1, {"state": "open"}, False, True),
        ({"state": "claimed", "claimed_by": "a"}, True, 1, None, True, True),
        ({"state": "claimed", "claimed_by": "b", "claim_generation": 2}, True, 1, None, False, False),
        ({"state": "open", "claimed_by": "a", "claim_generation": 2}, True, 1, None, False, False),
        ({"state": "pr", "claimed_by": "a", "awaiting": "merge", "claim_generation": 2}, True, 1, None, False, False),
    ],
)
def test_drop_releases_the_claim_refunds_a_held_life_and_reopens_the_task(
    monkeypatch: pytest.MonkeyPatch,
    row: dict[str, object],
    refund: bool,
    claims: int,
    live_row: dict[str, object] | None,
    refunded: bool,
    reopened: bool,
) -> None:
    settled: list[tuple[object, ...]] = []
    monkeypatch.setattr(tick, "InboxStore", lambda redis: ("inbox", redis))
    monkeypatch.setattr(tick.exits, "settle", lambda *args: settled.append(args))
    monkeypatch.setattr(tick, "awaiting_merge", lambda task: task.get("awaiting") == "merge")
    store = _DropStore(claims)
    expected = {"claimed_by": "a", "claim_generation": row.get("claim_generation", 0)}
    ledger = _DropLedger({"state": "open", "claimed_by": ""}, live_row or {**row, **expected})
    agent = SimpleNamespace(name="a", task="t1", seat="eng-1@slug")
    rows: dict[str, dict[str, object]] = {"t1": dict(row)}
    options = {"refund": True} if refund else {}
    assert common._drop("slug", store, ledger, rows, agent, **options) == (", task t1 reopened" if reopened else "")
    held = row["state"] in tick.ACTIVE and row["claimed_by"] == "a" and row.get("awaiting") != "merge"
    calls: list[tuple[object, ...]] = [("claims", "slug", "t1")] if refund and held else []
    calls.append(("claim_generation", "slug", "t1"))
    if refunded:
        calls.append(("refund", "slug", "t1", "a", 5))
    calls += [("release", "slug", "t1", "a"), ("drop_agent", "slug", "a")]
    if reopened:
        calls.append(("keep_seat", "slug", "t1", "eng-1@slug"))
    assert store.calls == calls
    ledger_calls: list[tuple[object, ...]] = [("task", "slug", "t1")] if refund and held and claims > 0 else []
    if held:
        ledger_calls.append(
            ("slug", "t1", {"state": "open", "claimed_by": ""}, {"if_state": tick.ACTIVE, "if_match": expected})
        )
    assert ledger.calls == ledger_calls
    assert settled == [(("inbox", "redis"), "a", "eng-1@slug" if reopened else "", "stopped")]


@pytest.mark.feature("swarm-tick-common-helpers")
def test_drop_releases_an_agent_whose_task_has_no_row(monkeypatch: pytest.MonkeyPatch) -> None:
    settled: list[tuple[object, ...]] = []
    monkeypatch.setattr(tick, "InboxStore", lambda redis: ("inbox", redis))
    monkeypatch.setattr(tick.exits, "settle", lambda *args: settled.append(args))
    store = _DropStore(1)
    ledger = _DropLedger({"state": "open", "claimed_by": ""}, {})
    agent = SimpleNamespace(name="a", task="gone", seat="eng-1@slug")
    assert common._drop("slug", store, ledger, {}, agent, refund=True) == ""
    assert store.calls == [
        ("claim_generation", "slug", "gone"),
        ("release", "slug", "gone", "a"),
        ("drop_agent", "slug", "a"),
    ]
    assert ledger.calls == []
    assert settled == [(("inbox", "redis"), "a", "", "stopped")]


class _CollectRedis:
    """Redis fake that records every set the collect helper makes."""

    def __init__(self) -> None:
        self.sets: list[tuple[object, str]] = []

    def set(self, key: object, value: str) -> None:
        self.sets.append((key, value))


class _CollectStore:
    """Store fake whose keys name the slug and the suffix they were built from."""

    def __init__(self) -> None:
        self.redis = _CollectRedis()

    def key(self, slug: str, suffix: str) -> tuple[str, str]:
        return (slug, suffix)


@pytest.mark.feature("swarm-tick-common-helpers")
@pytest.mark.parametrize(("lane", "recorded"), [(tick.MASTER, True), ("eng", False)])
def test_collected_records_a_masters_retired_tasks_then_drops_the_agent(
    monkeypatch: pytest.MonkeyPatch, lane: str, recorded: bool
) -> None:
    dropped: list[tuple[object, ...]] = []
    monkeypatch.setattr(tick, "_drop", lambda *args: dropped.append(args) or ", dropped")
    store, ledger = _CollectStore(), object()
    rows: dict[str, Any] = {"t1": {"state": "claimed"}, "t2": {"state": "open"}}
    agent = SimpleNamespace(name="a", task="t1", lane=lane)
    assert common._collected("slug", store, ledger, rows, agent) == ", dropped"
    assert dropped == [("slug", store, ledger, rows, agent)]
    expected = [(("slug", "master-retired-tasks"), '["t1", "t2"]')] if recorded else []
    assert store.redis.sets == expected
