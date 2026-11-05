"""The place step's failed launch outcomes release the launch, report capacity and route waits through the tick seams."""

import json
import os
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.swarm import capacity, quota_handoff, tick
from scripts.swarm.profile_choice import ProfileUnresolved
from scripts.tick.steps import place

pytestmark = pytest.mark.unit


class _Store:
    """Record every launch the outcome writes."""

    def __init__(self) -> None:
        self.recorded = list[tuple[Any, ...]]()

    def record_launch(self, *args: Any) -> None:
        self.recorded.append(args)


def _record() -> SimpleNamespace:
    return SimpleNamespace(name="engineer@1-1", lane="eng")


def _failure_seams(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    calls: list[tuple[Any, ...]] = []
    for name in ("_release_admission", "_release_hive_launch", "_record_spawn_failure"):
        monkeypatch.setattr(tick, name, lambda *args, name=name: calls.append((name, args)))
    monkeypatch.setattr(tick, "_drop", lambda *args: calls.append(("_drop", args)) or " and dropped")
    monkeypatch.setattr(tick, "_unresolved", lambda *args: calls.append(("_unresolved", args)) or "unresolved")
    return calls


@pytest.mark.feature("swarm-tick-launch-cluster")
def test_a_failed_spawn_releases_its_launch_and_reports_the_drop(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _failure_seams(monkeypatch)
    record, task, error = _record(), {"id": "t1"}, tick.SpawnError("pane gone")
    rows: dict[str, Any] = {"t1": {}}
    actions = place._spawn_failed("sw", "store", "ledger", "runtime", rows, place.Failed(record, task, error), 7)
    assert actions == ["spawn failed for t1 and dropped: pane gone"]
    assert calls == [
        ("_release_admission", ("sw", "runtime", task)),
        ("_release_hive_launch", ("sw", "store", record)),
        ("_record_spawn_failure", ("sw", "store", record, error, 7)),
        ("_drop", ("sw", "store", "ledger", rows, record)),
    ]


@pytest.mark.feature("swarm-tick-launch-cluster")
def test_an_unresolved_profile_spawn_also_blocks_its_task(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _failure_seams(monkeypatch)
    record, rows = _record(), {"t1": {}}
    error = ProfileUnresolved("no profile fits")
    actions = place._spawn_failed(
        "sw", "store", "ledger", "runtime", rows, place.Failed(record, {"id": "t1"}, error), 7
    )
    assert actions == ["spawn failed for t1 and dropped: no profile fits", "unresolved"]
    assert calls[-1] == ("_unresolved", ("sw", "ledger", rows, "t1", "no profile fits"))


@pytest.mark.feature("swarm-tick-launch-cluster")
@pytest.mark.parametrize(("observed_at", "listed"), [(7, ["fresh"]), (6, [])])
def test_a_full_account_reservation_reports_the_accounts_with_room(
    monkeypatch: pytest.MonkeyPatch, observed_at: int, listed: list[str]
) -> None:
    _failure_seams(monkeypatch)
    rows = [{"name": "fresh"}, {"name": "spent"}]
    read: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        capacity,
        "read",
        lambda store, slug: read.append((store, slug)) or {"observed_at": observed_at, "accounts": rows},
    )
    monkeypatch.setattr(capacity, "Account", lambda **row: SimpleNamespace(**row))
    monkeypatch.setattr(
        quota_handoff.Thresholds,
        "from_env",
        classmethod(lambda cls, env: "thresholds" if env == dict(os.environ) else "unread"),
    )
    monkeypatch.setattr(
        quota_handoff,
        "trigger",
        lambda account, thresholds: "handoff" if account.name == "spent" or thresholds != "thresholds" else "",
    )
    error = tick.SpawnError("account reservation is full for claude", harness="claude")
    actions = place._spawn_failed(
        "sw", "store", "ledger", "runtime", {}, place.Failed(_record(), {"id": "t1"}, error), 7
    )
    snapshot = {"harness": "claude", "accounts": [row for row in rows if row["name"] in listed]}
    assert actions == [
        f"spawn capacity for t1: {json.dumps(snapshot)}",
        "spawn failed for t1 and dropped: account reservation is full for claude",
    ]
    assert read == [("store", "sw")]


def _wait_seams(monkeypatch: pytest.MonkeyPatch, room: str | bool, reservation: str) -> list[tuple[Any, ...]]:
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(tick, "_acknowledging_error", lambda exc: str(exc) == "pending")
    monkeypatch.setattr(tick, "_failed_room_wait", lambda *args: calls.append(("room", args)) or room)
    monkeypatch.setattr(tick, "_refused_reservation", lambda *args: calls.append(("reservation", args)) or reservation)
    monkeypatch.setattr(tick, "_room_refused", lambda *args: calls.append(("refused", args)) or "room refused")
    monkeypatch.setattr(tick, "_spawn_failed", lambda *args: calls.append(("failed", args)) or ["spawn failed"])
    return calls


@pytest.mark.feature("swarm-tick-launch-cluster")
def test_a_pending_launch_waits_for_its_hive_acknowledgement(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _wait_seams(monkeypatch, False, "")
    store, record = _Store(), _record()
    failed = place.Failed(record, {"id": "t1"}, RuntimeError("pending"))
    actions = place._not_launched("sw", store, "ledger", "runtime", {}, failed, 7)
    assert actions == ["engineer@1-1 waits for the hive acknowledgement of t1"]
    assert store.recorded == [("sw", record, tick.ACKNOWLEDGING)]
    assert calls == []


@pytest.mark.feature("swarm-tick-launch-cluster")
@pytest.mark.parametrize(
    ("room", "reservation", "actions", "steps"),
    [
        ("task t1 waits: hive full", "", ["room refused"], ["room", "refused"]),
        (False, "task t1 waits: reserved", ["room refused"], ["room", "reservation", "refused"]),
        (False, "", ["spawn failed"], ["room", "reservation", "failed"]),
    ],
)
def test_a_refused_launch_waits_for_room_or_fails(
    monkeypatch: pytest.MonkeyPatch, room: str | bool, reservation: str, actions: list[str], steps: list[str]
) -> None:
    calls = _wait_seams(monkeypatch, room, reservation)
    store, record, task, error = _Store(), _record(), {"id": "t1"}, RuntimeError("refused")
    rows: dict[str, Any] = {"t1": {}}
    failed = place.Failed(record, task, error)
    assert place._not_launched("sw", store, "ledger", "runtime", rows, failed, 7) == actions
    assert [step for step, _ in calls] == steps
    assert calls[0] == ("room", ("runtime", "eng", task))
    expected = {
        "reservation": (task, error),
        "refused": ("sw", store, "ledger", "runtime", rows, failed, room or reservation),
        "failed": ("sw", store, "ledger", "runtime", rows, failed, 7),
    }
    assert all(args == expected[step] for step, args in calls[1:])
    assert store.recorded == []
