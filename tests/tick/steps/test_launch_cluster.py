"""The launch cluster lives in the observe and place step modules and the swarm tick module re-exports each name."""

from collections.abc import Callable
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from scripts.swarm import dispatcher, launch_check, ledger_events, profile_first, seat_spawn, tick
from scripts.swarm.store import MASTER, AgentRecord
from scripts.swarm_v2.admission import ADMITTED, DEFERRED, IMPOSSIBLE, REPLAYED
from scripts.tick.steps import observe, place

pytestmark = pytest.mark.unit

OBSERVED = (
    "_verify",
    "_launch_checks",
    "_failed_hive_launch",
    "_failed_launch",
    "_retire_launch",
    "_acknowledging",
)
PLACED = (
    "Started",
    "_started",
    "Failed",
    "_spawn_failed",
    "_not_launched",
    "_failed_room_wait",
    "_refused_reservation",
    "_room_refused",
    "_room_wait",
    "_admission",
    "_held_back",
    "_settle_admission",
    "_release_admission",
)
MOVED = [(observe, name) for name in OBSERVED] + [(place, name) for name in PLACED]


@pytest.mark.feature("swarm-tick-launch-cluster")
@pytest.mark.parametrize(("module", "name"), MOVED)
def test_tick_reexports_the_moved_launch_name(module: ModuleType, name: str) -> None:
    moved = getattr(module, name)
    assert moved.__module__ == module.__name__
    assert getattr(tick, name) is moved


@pytest.mark.feature("swarm-tick-launch-cluster")
def test_the_tick_module_holds_no_launch_definition() -> None:
    text = Path(str(tick.__file__)).read_text(encoding="utf-8")
    assert [name for _, name in MOVED if f"\ndef {name}(" in text or f"\nclass {name}:" in text] == []


@pytest.mark.feature("swarm-tick-launch-cluster")
def test_moved_launch_code_reaches_tick_seams_through_the_tick_module(monkeypatch: pytest.MonkeyPatch) -> None:
    released: list[tuple[str, str, str]] = []
    gate = SimpleNamespace(release=lambda slug, task_id, reservation: released.append((slug, task_id, reservation)))
    monkeypatch.setattr(tick, "_admission", lambda runtime: gate if runtime == "runtime" else None)
    place._release_admission("slug", "runtime", {"id": "t1", "admission": {"reservation": "r1"}})
    assert released == [("slug", "t1", "r1")]


@pytest.mark.feature("swarm-tick-launch-cluster")
def test_an_acknowledging_check_reads_the_launch_through_the_tick_master_lane(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = SimpleNamespace(lane=MASTER, name="master@1-1")
    assert observe._acknowledging("slug", "store", agent) is False


class _Recorder:
    """Record each method call made on it into a shared list and answer from a table keyed by method name."""

    redis: "_Recorder"

    def __init__(self, calls: list[tuple[Any, ...]], answers: dict[str, Any], prefix: str = "") -> None:
        self.calls, self.answers, self.prefix = calls, answers, prefix

    def __getattr__(self, name: str) -> Callable[..., Any]:
        def call(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((self.prefix + name, args, kwargs))
            return self.answers.get(name)

        return call


def _start(
    monkeypatch: pytest.MonkeyPatch, restart: str | None, pr_url: str, head: str
) -> tuple[list[tuple[Any, ...]], list[str], Any, Any, Any]:
    calls: list[tuple[Any, ...]] = []
    store = _Recorder(calls, {"take_restart": restart, "key": "sw:launch-assignments"})
    store.redis = _Recorder(calls, {}, "redis.")
    fake = _Recorder(calls, {"_master_set": "master-after", "head": head, "chain_claimed": ["chained"]})
    for name in ("_master_set", "_spend_local"):
        monkeypatch.setattr(tick, name, getattr(fake, name))
    monkeypatch.setattr(ledger_events, "head", fake.head)
    monkeypatch.setattr(launch_check, "begin", fake.begin)
    monkeypatch.setattr(profile_first, "spawned", fake.spawned)
    monkeypatch.setattr(dispatcher, "chain_claimed", fake.chain_claimed)
    record, launched = AgentRecord("engineer@1-1", "eng", "t1"), AgentRecord("engineer@1-1", "eng", "t1", pane_id="%9")
    task = {"id": "t1", "pr_url": pr_url} if pr_url else {"id": "t1"}
    started = place.Started(record, task, SimpleNamespace(pane_id="%9", harness="claude"), launched)
    actions = place._started("sw", "config", store, "ledger", {"doc": 1}, started, 5)
    return calls, actions, store, record, launched


@pytest.mark.feature("swarm-tick-launch-cluster")
@pytest.mark.parametrize(
    ("restart", "pr_url", "head", "after", "recorded"),
    [
        ("restart-after", "https://pr/1", "", "restart-after", None),
        (None, "", "unused", "master-after", ""),
        ("restart-after", "https://pr/1", "abc", "restart-after", "abc"),
    ],
)
def test_a_started_launch_records_its_claim_launch_spend_and_chain(
    monkeypatch: pytest.MonkeyPatch, restart: str | None, pr_url: str, head: str, after: str, recorded: str | None
) -> None:
    calls, actions, store, record, launched = _start(monkeypatch, restart, pr_url, head)
    expected: list[tuple[Any, ...]] = [
        ("count_claim", ("sw", "t1"), {}),
        ("take_restart", ("sw", "t1"), {}),
        *([] if restart else [("_master_set", ({"doc": 1}, "t1"), {})]),
        *([("head", (pr_url,), {})] if pr_url else []),
        ("record_launch", ("sw", launched, "started"), {"after": after, "head": recorded}),
        ("put_agent", ("sw", launched), {}),
        ("begin", (store, "sw", record, 5), {}),
        ("count_spawn", ("sw", "claude"), {}),
        ("_spend_local", (store, launched, 5), {}),
        ("clear_handoff", ("sw", "t1"), {}),
        ("clear_failed_harness", ("sw", "t1"), {}),
        ("key", ("sw", "launch-assignments"), {}),
        ("redis.hdel", ("sw:launch-assignments", "t1"), {}),
        ("spawned", ("sw", store, "t1"), {}),
        (
            "chain_claimed",
            ("sw", "config", "ledger", {"doc": 1}, {"id": "t1", **({"pr_url": pr_url} if pr_url else {})}, "eng", 5),
            {},
        ),
    ]
    assert calls == expected
    assert actions == ["spawned engineer@1-1 for t1", "chained"]


@pytest.mark.feature("swarm-tick-launch-cluster")
@pytest.mark.parametrize(
    ("outcome", "said", "admission"),
    [
        (ADMITTED, "", {"reservation": "r1", "deadline_ms": 9}),
        (REPLAYED, "", {"reservation": "r1", "deadline_ms": 9}),
        (DEFERRED, "task t1 waits: later", None),
        (IMPOSSIBLE, "blocked sw t1 later", None),
    ],
)
def test_a_held_back_task_follows_its_admission_decision(
    monkeypatch: pytest.MonkeyPatch, outcome: str, said: str, admission: dict[str, Any] | None
) -> None:
    admitted: list[tuple[Any, ...]] = []
    decision = SimpleNamespace(outcome=outcome, reason="later", reservation="r1", deadline_ms=9)
    gate = SimpleNamespace(admit=lambda *args: admitted.append(args) or [decision])
    ledger, rows, task = object(), {"t1": {}}, {"id": "t1"}
    monkeypatch.setattr(tick, "_admission", lambda runtime: gate if runtime == "runtime" else None)

    def unresolved(slug: str, seen_ledger: object, seen_rows: dict[str, Any], task_id: str, reason: str) -> str:
        assert (seen_ledger, seen_rows) == (ledger, rows)
        return f"blocked {slug} {task_id} {reason}"

    monkeypatch.setattr(tick, "_unresolved", unresolved)
    assert place._held_back("sw", ledger, rows, "runtime", task, 7) == said
    assert admitted == [("sw", [task], 7)]
    assert task.get("admission") == admission


@pytest.mark.feature("swarm-tick-launch-cluster")
def test_a_task_without_an_admission_gate_or_with_a_refusing_gate_is_held_or_passed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(*args: Any) -> Any:
        raise tick.SwarmError("pending admission conflicted")

    monkeypatch.setattr(tick, "_admission", lambda runtime: None)
    assert place._held_back("sw", None, {}, "runtime", {"id": "t1"}, 7) == ""
    monkeypatch.setattr(tick, "_admission", lambda runtime: SimpleNamespace(admit=refuse))
    assert place._held_back("sw", None, {}, "runtime", {"id": "t1"}, 7) == "task t1 waits: pending admission conflicted"


@pytest.mark.feature("swarm-tick-launch-cluster")
@pytest.mark.parametrize(("placed", "said"), [("hive full", "task t1 waits: hive full"), ("", ""), (None, "")])
def test_a_room_wait_names_the_placement_refusal(
    monkeypatch: pytest.MonkeyPatch, placed: str | None, said: str
) -> None:
    seen: list[tuple[Any, ...]] = []

    def placed_elsewhere(*args: Any) -> str:
        seen.append(args)
        if placed is None:
            raise tick.ProfileUnresolved("no profile")
        return placed

    monkeypatch.setattr(seat_spawn, "placed_elsewhere", placed_elsewhere)
    task = {"id": "t1"}
    assert place._room_wait("runtime", "eng", task) == said
    assert seen == [("runtime", "eng", task)]
