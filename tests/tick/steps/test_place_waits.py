"""The place step's room waits and admission settling act through the tick seams with the launch they hold."""

from types import SimpleNamespace
from typing import Any

import pytest

from scripts.swarm import capacity, quota_handoff, tick
from scripts.tick.steps import place

pytestmark = pytest.mark.unit


@pytest.mark.feature("swarm-tick-launch-cluster")
def test_a_room_refused_launch_releases_its_holds_and_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []
    for name in ("_release_admission", "_release_hive_launch", "_drop"):
        monkeypatch.setattr(tick, name, lambda *args, name=name: calls.append((name, args)) or "")
    monkeypatch.setattr(place.transfers, "detach", lambda *args: calls.append(("detach", args)))
    store = SimpleNamespace(record_launch=lambda *args: calls.append(("record_launch", args)))
    record, task, rows = SimpleNamespace(name="engineer@1-1"), {"id": "t1"}, {"t1": {}}
    failed = place.Failed(record, task, RuntimeError("hive full"))
    said = place._room_refused("sw", store, "ledger", "runtime", rows, failed, "task t1 waits: hive full")
    assert said == "task t1 waits: hive full"
    assert calls == [
        ("_release_admission", ("sw", "runtime", task)),
        ("_release_hive_launch", ("sw", store, record)),
        ("detach", (store, "sw", record)),
        ("record_launch", ("sw", record, "waiting", "hive full")),
        ("_drop", ("sw", store, "ledger", rows, record)),
    ]


@pytest.mark.feature("swarm-tick-launch-cluster")
@pytest.mark.parametrize(("answer", "said"), [("task t1 waits: full", "task t1 waits: full"), (None, False)])
def test_a_failed_room_wait_reads_a_spawn_error_as_no_wait(
    monkeypatch: pytest.MonkeyPatch, answer: str | None, said: str | bool
) -> None:
    asked: list[tuple[Any, ...]] = []

    def room_wait(*args: Any) -> str:
        asked.append(args)
        if answer is None:
            raise tick.SpawnError("no room")
        return answer

    monkeypatch.setattr(tick, "_room_wait", room_wait)
    task = {"id": "t1"}
    assert place._failed_room_wait("runtime", "eng", task) == said
    assert asked == [("runtime", "eng", task)]


@pytest.mark.feature("swarm-tick-launch-cluster")
@pytest.mark.parametrize(
    ("has_gate", "held", "activated"), [(True, "r1", True), (True, "", False), (False, "r1", False)]
)
def test_a_settled_launch_activates_its_held_reservation(
    monkeypatch: pytest.MonkeyPatch, has_gate: bool, held: str, activated: bool
) -> None:
    calls: list[tuple[Any, ...]] = []
    gate = SimpleNamespace(
        held=lambda slug, task: calls.append(("held", slug, task)) or held,
        activate=lambda *args: calls.append(("activate", args)),
    )
    monkeypatch.setattr(tick, "_admission", lambda runtime: gate if has_gate and runtime == "runtime" else None)
    place._settle_admission("sw", "runtime", SimpleNamespace(task="t1"))
    expected: list[tuple[Any, ...]] = [("held", "sw", "t1")] if has_gate else []
    assert calls == expected + ([("activate", ("sw", "t1", "r1"))] if activated else [])


@pytest.mark.feature("swarm-tick-launch-cluster")
def test_a_full_reservation_read_without_accounts_reports_none(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("_release_admission", "_release_hive_launch", "_record_spawn_failure"):
        monkeypatch.setattr(tick, name, lambda *args: None)
    monkeypatch.setattr(tick, "_drop", lambda *args: "")
    monkeypatch.setattr(capacity, "read", lambda store, slug: {"observed_at": 7})
    monkeypatch.setattr(quota_handoff.Thresholds, "from_env", classmethod(lambda cls, env: "thresholds"))
    error = tick.SpawnError("account reservation is full", harness="codex")
    failed = place.Failed(SimpleNamespace(name="engineer@1-1"), {"id": "t1"}, error)
    actions = place._spawn_failed("sw", "store", "ledger", "runtime", {}, failed, 7)
    assert actions[0] == 'spawn capacity for t1: {"harness": "codex", "accounts": []}'
