"""The master placement operations live in the masters step module, reach tick names through the tick module, and
the swarm tick module re-exports each one."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.swarm import controller, lease, tick
from scripts.swarm.store import MASTER, AgentRecord
from scripts.tick.steps import masters

pytestmark = pytest.mark.unit

MOVED = ("_master", "_recover_master", "_retire_master", "respawn_master", "place_master", "_under_tick_lock")
SEAT = "master@sw"
CODE = "abc123"
NOW = 1_000


class Calls(list[tuple[Any, ...]]):
    """Every fake call in order, as its name and arguments."""

    def fn(self, name: str, result: Any = None) -> Callable[..., Any]:
        """Return a fake that records its call under ``name`` and answers ``result``."""

        def call(*args: Any, **kwargs: Any) -> Any:
            """Record the call and answer the fixed result."""
            self.append((name, args, kwargs) if kwargs else (name, args))
            return result

        return call

    def names(self) -> list[str]:
        """Return the names of the recorded calls in order."""
        return [call[0] for call in self]


def _given(result: Callable[..., Any]) -> Callable[..., Any]:
    """Return a fake that refuses a missing argument and answers ``result`` called with its arguments."""

    def call(*args: Any, **kwargs: Any) -> Any:
        """Refuse a None argument, then answer the result."""
        assert None not in args
        assert None not in kwargs.values()
        return result(*args, **kwargs)

    return call


def _master_record(name: str = "master@abc123-0001", **fields: Any) -> AgentRecord:
    """Return a lead master record named ``name`` with ``fields`` replaced."""
    base: dict[str, Any] = {"name": name, "lane": MASTER, "task": MASTER, "seat": SEAT}
    return AgentRecord(**(base | fields))


def _store(
    calls: Calls,
    agents: list[AgentRecord] | None = None,
    occupant: str = "",
    occupants: dict[str, AgentRecord] | None = None,
) -> Any:
    """Return a store holding ``agents`` whose writes are recorded."""
    redis = SimpleNamespace(hdel=calls.fn("hdel"), hset=calls.fn("hset"), delete=calls.fn("delete"))
    seats = SimpleNamespace(occupant=lambda seat: SimpleNamespace(occupant=occupant), occupy=calls.fn("occupy"))
    return SimpleNamespace(
        redis=redis,
        seats=seats,
        agents=_given(lambda slug: list(agents or [])),
        execution_occupants=_given(lambda slug: dict(occupants or {})),
        put_agent=calls.fn("put_agent"),
        drop_agent=calls.fn("drop_agent"),
        clear_handoff=calls.fn("clear_handoff"),
        handoff=_given(lambda slug, task: "handoff text"),
        peer=_given(lambda slug: "peer"),
        key=lambda slug, kind: f"{slug}/{kind}",
    )


def _runtime(calls: Calls, live: set[str] | None = None, reported: bool = True, retired: bool = True) -> Any:
    """Return a runtime with ``live`` names whose retires and recoveries are recorded."""
    return SimpleNamespace(
        live_names=lambda: set(live or ()),
        reported=lambda record: reported,
        recover=lambda name: f"placed {name}",
        retire=calls.fn("retire", retired),
    )


@pytest.fixture
def placed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the tick placed record copy the placement into the pane field."""
    monkeypatch.setattr(tick, "placed_record", lambda record, placed: replace(record, pane_id=str(placed)))


@pytest.mark.feature("swarm-tick-master-placement")
@pytest.mark.parametrize("name", MOVED)
def test_tick_reexports_the_moved_master_operation(name: str) -> None:
    moved = getattr(masters, name)
    assert moved.__module__ == "scripts.tick.steps.masters"
    assert getattr(tick, name) is moved


@pytest.mark.feature("swarm-tick-master-placement")
def test_the_tick_module_holds_no_moved_master_operation() -> None:
    text = Path(str(tick.__file__)).read_text(encoding="utf-8")
    assert [name for name in MOVED if f"\ndef {name}(" in text] == []


@pytest.mark.feature("swarm-tick-master-placement")
def test_retire_master_returns_the_hold_without_retiring(monkeypatch: pytest.MonkeyPatch) -> None:
    calls, seen = Calls(), []

    def hold(store: Any, slug: str, master: Any, reason: str, live: bool, now_ms: int) -> str:
        seen.append((slug, master.name, reason, live, now_ms))
        return "held"

    monkeypatch.setattr(tick.master_retire, "hold", _given(hold))
    master = _master_record()
    assert masters._retire_master("sw", _store(calls), _runtime(calls, {master.name}), master, NOW) == "held"
    assert seen == [("sw", master.name, "the swarm is stopping", True, NOW)]
    assert calls == []


@pytest.mark.feature("swarm-tick-master-placement")
@pytest.mark.parametrize(
    ("retired", "said", "dropped"),
    [
        (True, "retired master@abc123-0001", True),
        (False, "could not retire master@abc123-0001, retrying next tick", False),
    ],
)
def test_retire_master_drops_the_master_only_once_retired(
    monkeypatch: pytest.MonkeyPatch, retired: bool, said: str, dropped: bool
) -> None:
    calls = Calls()
    monkeypatch.setattr(tick.master_retire, "hold", _given(lambda *args: None))
    monkeypatch.setattr(tick.reaper, "scratch_homes", lambda slug, task: [f"{slug}:{task}"])
    master = _master_record()
    runtime = _runtime(calls, retired=retired)
    assert masters._retire_master("sw", _store(calls), runtime, master, NOW) == said
    assert calls[0] == ("retire", (master,), {"homes": ["sw:master"]})
    assert (("drop_agent", ("sw", master.name)) in calls) is dropped


@pytest.mark.feature("swarm-tick-master-placement")
def test_retire_master_asks_the_hive_report_with_its_store_master_homes_and_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = Calls()
    seen: list[tuple[Any, ...]] = []

    def gone(store: Any, runtime: Any, master: Any, homes: Any, now_ms: int) -> bool:
        seen.append((store, runtime, master, homes, now_ms))
        return True

    monkeypatch.setattr(tick.master_retire, "hold", _given(lambda *args: None))
    monkeypatch.setattr(tick.reaper, "scratch_homes", lambda slug, task: [f"{slug}:{task}"])
    monkeypatch.setattr(tick.retire_watch, "retire_unless_gone", gone)
    master, store = _master_record(), _store(calls)
    runtime = _runtime(calls)
    assert masters._retire_master("sw", store, runtime, master, NOW) == f"retired {master.name}"
    assert seen == [(store, runtime, master, ["sw:master"], NOW)]


@pytest.mark.feature("swarm-tick-master-placement")
def test_a_stopping_swarm_waits_for_its_workers_then_retires_each_master(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = Calls()
    monkeypatch.setattr(
        tick, "_retire_master", _given(lambda slug, store, runtime, master, now_ms: f"retired {master.name}")
    )
    config = SimpleNamespace(state="stopping")
    worker = AgentRecord("engineer@abc123-0002", "eng", "t1")
    first, second = _master_record(), _master_record("master@abc123-0003", seat="master-2@sw")
    busy = _store(calls, [first, worker])
    assert masters._master("sw", config, busy, _runtime(calls), NOW) == []
    assert calls == [("hdel", (tick.MASTER_WAITING, "sw"))]
    idle = _store(calls, [first, second])
    assert masters._master("sw", config, idle, _runtime(calls), NOW) == [
        f"retired {first.name}",
        f"retired {second.name}",
    ]


def _starts(monkeypatch: pytest.MonkeyPatch, pending: dict[str, Any], blocked: bool = False) -> Calls:
    """Patch the master start record to answer ``pending`` and ``blocked`` and record its writes."""
    calls = Calls()
    start = SimpleNamespace(
        read=_given(lambda store, slug: pending),
        blocked=_given(lambda found, now_ms: blocked),
        begin=calls.fn("begin"),
        withdraw=calls.fn("withdraw"),
        failed=calls.fn("start failed", ["master start failed"]),
    )
    monkeypatch.setattr(tick, "master_start", start)
    return calls


def _seats(
    monkeypatch: pytest.MonkeyPatch,
    elsewhere: str = "",
    slot: str = "",
    host: str = "",
    place: Callable[..., Any] | None = None,
    room: bool = False,
) -> None:
    """Patch the seat spawn module with the given refusals and placement."""
    spawn = SimpleNamespace(
        placed_elsewhere=_given(lambda runtime, lane, held: elsewhere),
        no_slot=_given(lambda config, runtime, lane: slot),
        host_hold=_given(lambda slug, store, now_ms, lane: host),
        place=_given(place) if place else None,
        room_refused=_given(lambda exc: room),
        SeatFailed=tick.seat_spawn.SeatFailed,
    )
    monkeypatch.setattr(tick, "seat_spawn", spawn)


@pytest.mark.feature("swarm-tick-master-placement")
@pytest.mark.parametrize(
    ("agents", "blocked", "live", "said"),
    [
        ([_master_record(state="working")], False, set(), []),
        ([], True, set(), []),
        (
            [_master_record(state="finished")],
            False,
            {"master@abc123-0001"},
            ["the old master is still running, waiting for it to end before starting the next"],
        ),
    ],
)
def test_a_master_starts_only_once_the_old_lead_ended(
    monkeypatch: pytest.MonkeyPatch, agents: list[AgentRecord], blocked: bool, live: set[str], said: list[str]
) -> None:
    calls = Calls()
    _starts(monkeypatch, {}, blocked)
    config = SimpleNamespace(state="running")
    assert masters._master("sw", config, _store(calls, agents), _runtime(calls, live), NOW) == said


@pytest.mark.feature("swarm-tick-master-placement")
def test_a_seated_non_lead_master_never_holds_the_start(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = Calls()
    _starts(monkeypatch, {})
    _seats(monkeypatch, elsewhere="placed on another hive")
    other = _master_record(state="working", seat="master-2@sw")
    config = SimpleNamespace(state="running")
    assert masters._master("sw", config, _store(calls, [other]), _runtime(calls), NOW) == ["placed on another hive"]


@pytest.mark.feature("swarm-tick-master-placement")
def test_a_master_without_a_slot_is_recorded_as_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = Calls()
    _starts(monkeypatch, {})
    _seats(monkeypatch, slot="no slot for the master")
    config = SimpleNamespace(state="running")
    assert masters._master("sw", config, _store(calls), _runtime(calls), NOW) == ["no slot for the master"]
    assert calls == [("hdel", (tick.MASTER_WAITING, "sw")), ("hset", (tick.MASTER_WAITING, "sw", NOW))]


@pytest.mark.feature("swarm-tick-master-placement")
@pytest.mark.parametrize(("past_host", "said"), [(False, ["host held"]), (True, ["the master waits: no room"])])
def test_a_host_hold_stops_the_master_unless_it_starts_past_the_host(
    monkeypatch: pytest.MonkeyPatch, past_host: bool, said: list[str]
) -> None:
    calls = Calls()
    _starts(monkeypatch, {})
    record = _master_record()
    monkeypatch.setattr(tick, "transfers", SimpleNamespace(detach=calls.fn("detach")))

    def place(*args: Any) -> Any:
        raise tick.seat_spawn.SeatFailed(record, RuntimeError("no room"))

    _seats(monkeypatch, host="host held", place=place, room=True)
    config = SimpleNamespace(state="running")
    store, runtime = _store(calls), _runtime(calls)
    started = masters._master("sw", config, store, runtime, NOW, past_host=True) if past_host else None
    assert (started or masters._master("sw", config, store, runtime, NOW)) == said


@pytest.mark.feature("swarm-tick-master-placement")
def test_a_master_refused_for_room_withdraws_its_start(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = Calls()
    starts = _starts(monkeypatch, {"task": {"id": MASTER}})
    record = _master_record()
    monkeypatch.setattr(tick, "transfers", SimpleNamespace(detach=calls.fn("detach")))

    def place(*args: Any) -> Any:
        raise tick.seat_spawn.SeatFailed(record, RuntimeError("no room"))

    _seats(monkeypatch, place=place, room=True)
    store = _store(calls)
    config = SimpleNamespace(state="running")
    assert masters._master("sw", config, store, _runtime(calls), NOW) == ["the master waits: no room"]
    assert calls[1:] == [("drop_agent", ("sw", record.name)), ("detach", (store, "sw", record))]
    assert starts == [("withdraw", (store, "sw", {"task": {"id": MASTER}}))]


@pytest.mark.feature("swarm-tick-master-placement")
def test_a_failed_master_spawn_marks_the_transfer_and_affinity_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = Calls()
    starts = _starts(monkeypatch, {})
    record = _master_record()
    monkeypatch.setattr(tick, "transfers", SimpleNamespace(failed=calls.fn("transfer failed")))
    monkeypatch.setattr(tick, "affinity", SimpleNamespace(failed=calls.fn("affinity failed")))

    def place(*args: Any) -> Any:
        raise tick.seat_spawn.SeatFailed(record, RuntimeError("herdr down"))

    _seats(monkeypatch, place=place)
    store = _store(calls)
    config = SimpleNamespace(state="running")
    assert masters._master("sw", config, store, _runtime(calls), NOW) == ["master start failed"]
    assert calls[1:] == [
        ("drop_agent", ("sw", record.name)),
        ("transfer failed", (store, "sw", record)),
        ("affinity failed", (store, "sw", "herdr down")),
    ]
    assert starts == [("start failed", (store, "sw", "master spawn failed: herdr down", NOW))]


def _placement(monkeypatch: pytest.MonkeyPatch, calls: Calls, pending: dict[str, Any]) -> tuple[Calls, list[Any]]:
    """Patch a placement that prepares the master task and records each write, returning the start writes and the
    prepared tasks."""
    starts = _starts(monkeypatch, pending)
    prepared: list[Any] = []
    record = _master_record()

    def place(slug: str, config: Any, store: Any, runtime: Any, lane: str, now_ms: int, prepare: Any) -> Any:
        prepared.append(prepare(record))
        return record, SimpleNamespace(harness="claude")

    _seats(monkeypatch, place=place)
    monkeypatch.setattr(
        tick, "transfers", SimpleNamespace(attach=_given(lambda store, slug, record: f"transfer {record.name}"))
    )
    monkeypatch.setattr(
        tick,
        "affinity",
        SimpleNamespace(handed_off=calls.fn("handed_off"), placed=calls.fn("affinity placed")),
    )
    monkeypatch.setattr(
        tick,
        "master_move",
        SimpleNamespace(
            pinned=_given(lambda store, slug, task, transfer: {**task, "pinned": transfer}),
            placed=calls.fn("move placed"),
            account_moved=_given(lambda slug, launched, record: [f"moved {launched['id']}"]),
        ),
    )
    monkeypatch.setattr(tick, "primed", _given(lambda store, slug, seat, task: {**task, "primed": seat}))
    monkeypatch.setattr(tick, "launch_check", SimpleNamespace(begin=calls.fn("launch check")))
    monkeypatch.setattr("scripts.swarm.master_launch._filled", _given(lambda task, config: {**task, "filled": True}))
    return starts, prepared


@pytest.mark.feature("swarm-tick-master-placement")
@pytest.mark.usefixtures("placed")
def test_a_reported_master_is_working_and_clears_its_start(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = Calls()
    starts, prepared = _placement(monkeypatch, calls, {})
    store = _store(calls)
    config = SimpleNamespace(state="running")
    record = replace(_master_record(), pane_id="namespace(harness='claude')")
    said = masters._master("sw", config, store, _runtime(calls, reported=True), NOW)
    assert said == [f"spawned master {record.name}", "moved master"]
    task = {
        "id": MASTER,
        "handoff": "handoff text",
        "peer": "peer",
        "transfer": f"transfer {record.name}",
        "primed": SEAT,
        "pinned": f"transfer {record.name}",
        "filled": True,
    }
    assert prepared == [task]
    assert starts == [("begin", (store, "sw", record.name, task, NOW))]
    assert calls == [
        ("hdel", (tick.MASTER_WAITING, "sw")),
        ("handed_off", (store, "sw")),
        ("affinity placed", (store, "sw", "claude")),
        ("move placed", (store, "sw")),
        ("put_agent", ("sw", replace(record, state="working"))),
        ("launch check", (store, "sw", record, NOW)),
        ("delete", ("sw/master-start",)),
        ("clear_handoff", ("sw", MASTER)),
        ("hdel", ("sw/launch-assignments", MASTER)),
    ]


@pytest.mark.feature("swarm-tick-master-placement")
@pytest.mark.usefixtures("placed")
def test_an_unreported_master_is_starting_and_keeps_its_pending_start(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = Calls()
    pending = {"task": {"id": MASTER, "kept": "yes"}}
    starts, prepared = _placement(monkeypatch, calls, pending)
    store = _store(calls)
    config = SimpleNamespace(state="running")
    record = replace(_master_record(), pane_id="namespace(harness='claude')")
    assert masters._master("sw", config, store, _runtime(calls, reported=False), NOW) == [
        f"spawned master {record.name}",
        "moved master",
    ]
    task = {"id": MASTER, "kept": "yes", "transfer": f"transfer {record.name}", "pinned": f"transfer {record.name}"}
    assert prepared == [{**task, "filled": True}]
    assert len(starts) == 1
    assert ("put_agent", ("sw", replace(record, state="starting"))) in calls
    assert "delete" not in calls.names()
    assert "clear_handoff" not in calls.names()
    assert calls[-1] == ("hdel", ("sw/launch-assignments", MASTER))


@pytest.mark.feature("swarm-tick-master-placement")
def test_recover_master_leaves_a_live_lead_alone() -> None:
    calls = Calls()
    lead = _master_record(state="working")
    config = SimpleNamespace(code=CODE)
    assert masters._recover_master("sw", config, _store(calls, [lead]), _runtime(calls, {lead.name}), NOW) == []
    assert calls == []


@pytest.mark.feature("swarm-tick-master-placement")
def test_recover_master_finds_no_candidate_among_finished_or_foreign_names() -> None:
    calls = Calls()
    done = _master_record(state="finished")
    live = {done.name, "engineer@abc123-0002", "master@fedcba-0004", "other-master-1"}
    config = SimpleNamespace(code=CODE)
    assert masters._recover_master("sw", config, _store(calls, [done]), _runtime(calls, live), NOW) == []
    assert calls == []


@pytest.mark.feature("swarm-tick-master-placement")
@pytest.mark.usefixtures("placed")
def test_recover_master_adopts_a_live_master_of_this_swarm_code() -> None:
    calls = Calls()
    gone = _master_record("master@abc123-0001", state="working")
    other = _master_record("master@abc123-0005", state="working", seat="master-2@sw")
    name = "master@abc123-0007"
    config = SimpleNamespace(code=CODE)
    store = _store(calls, [gone, other])
    said = masters._recover_master("sw", config, store, _runtime(calls, {name, other.name}), NOW)
    assert said == [f"adopted live master {name}"]
    adopted = AgentRecord(name, MASTER, MASTER, started_at=NOW, seat=SEAT, pane_id=f"placed {name}")
    assert calls == [
        ("put_agent", ("sw", adopted)),
        ("drop_agent", ("sw", gone.name)),
        ("occupy", (SEAT, name, NOW)),
    ]


@pytest.mark.feature("swarm-tick-master-placement")
@pytest.mark.usefixtures("placed")
def test_recover_master_prefers_the_seat_occupant_and_keeps_its_execution_record() -> None:
    calls = Calls()
    occupant = "sw-master-9"
    held = _master_record(occupant, account="acct", started_at=5)
    config = SimpleNamespace(code=CODE)
    store = _store(calls, occupant=occupant, occupants={SEAT: held})
    live = {"master@abc123-0002", occupant}
    assert masters._recover_master("sw", config, store, _runtime(calls, live), NOW) == [
        f"adopted live master {occupant}"
    ]
    assert calls == [("put_agent", ("sw", replace(held, pane_id=f"placed {occupant}")))]


@pytest.mark.feature("swarm-tick-master-placement")
@pytest.mark.usefixtures("placed")
def test_recover_master_takes_the_first_named_candidate_and_replaces_a_stale_execution_record() -> None:
    calls = Calls()
    stale = _master_record("master@abc123-0001")
    finished = _master_record("master@abc123-0003", state="finished")
    config = SimpleNamespace(code=CODE)
    store = _store(calls, [finished], occupants={SEAT: stale})
    live = {"sw-master-2", "master@abc123-0004", finished.name}
    said = masters._recover_master("sw", config, store, _runtime(calls, live), NOW)
    assert said == ["adopted live master master@abc123-0004"]
    adopted = AgentRecord(
        "master@abc123-0004", MASTER, MASTER, started_at=NOW, seat=SEAT, pane_id="placed master@abc123-0004"
    )
    assert calls == [("put_agent", ("sw", adopted)), ("occupy", (SEAT, "master@abc123-0004", NOW))]


@pytest.mark.feature("swarm-tick-master-placement")
@pytest.mark.usefixtures("placed")
def test_recover_master_adopts_a_legacy_named_master_of_this_swarm() -> None:
    calls = Calls()
    config = SimpleNamespace(code=CODE)
    said = masters._recover_master("sw", config, _store(calls), _runtime(calls, {"sw-master-2"}), NOW)
    assert said == ["adopted live master sw-master-2"]
    adopted = AgentRecord("sw-master-2", MASTER, MASTER, started_at=NOW, seat=SEAT, pane_id="placed sw-master-2")
    assert calls == [("put_agent", ("sw", adopted)), ("occupy", (SEAT, "sw-master-2", NOW))]


@pytest.fixture
def unlocked(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    """Run each act under a fake tick lock with config ``running`` and record its slug, store and caller."""
    seen: list[tuple[Any, ...]] = []

    def locked(slug: str, store: Any, act: Any, caller: str) -> Any:
        seen.append((slug, caller))
        return act(SimpleNamespace(state="running", name="config"), "held")

    monkeypatch.setattr(tick, "_under_tick_lock", _given(locked))
    return seen


@pytest.mark.feature("swarm-tick-master-placement")
def test_place_master_starts_the_master_on_its_deployment_under_the_lock(
    monkeypatch: pytest.MonkeyPatch, unlocked: list[tuple[Any, ...]]
) -> None:
    fenced: list[tuple[Any, ...]] = []
    monkeypatch.setattr(controller, "deployment_for", lambda config, env: f"deploy {config.name}")
    monkeypatch.setattr(controller, "FencedRuntime", lambda *args: fenced.append(args) or "fenced")
    monkeypatch.setattr(
        tick, "_master", _given(lambda slug, config, store, runtime, now_ms: [f"{runtime} {config.name} {now_ms}"])
    )
    assert masters.place_master("sw", "store", "runtime", NOW) == [f"fenced config {NOW}"]
    assert fenced == [("store", "sw", "held", "runtime", "deploy config")]
    assert unlocked == [("sw", "place master")]


@pytest.mark.feature("swarm-tick-master-placement")
@pytest.mark.parametrize(("recovered", "said"), [(["adopted"], ["adopted"]), ([], ["spawned"])])
def test_respawn_master_drops_gone_leads_then_adopts_or_spawns(
    monkeypatch: pytest.MonkeyPatch, unlocked: list[tuple[Any, ...]], recovered: list[str], said: list[str]
) -> None:
    calls = Calls()
    gone = _master_record("master@abc123-0001", state="working")
    live = _master_record("master@abc123-0002", state="working")
    finished = _master_record("master@abc123-0003", state="finished")
    starting = _master_record("master@abc123-0004", state="starting")
    seated = _master_record("master@abc123-0005", state="working", seat="master-2@sw")
    worker = AgentRecord("engineer@abc123-0006", "eng", "t1")
    store = _store(calls, [gone, live, finished, starting, seated, worker])
    runtime = SimpleNamespace(live_names=lambda: {live.name})
    fenced: list[tuple[Any, ...]] = []
    monkeypatch.setattr(controller, "FencedRuntime", lambda *args: fenced.append(args) or runtime)
    monkeypatch.setattr(tick, "_recover_master", _given(lambda slug, config, store, runtime, now_ms: recovered))
    monkeypatch.setattr(tick, "_master", _given(lambda slug, config, store, runtime, now_ms: ["spawned"]))
    assert masters.respawn_master("sw", store, "runtime", NOW) == [f"dropped the gone master {gone.name}", *said]
    assert calls == [("drop_agent", ("sw", gone.name))]
    assert fenced == [(store, "sw", "held", "runtime", controller.LOCAL)]
    assert unlocked == [("sw", "respawn master")]


def _lock(
    monkeypatch: pytest.MonkeyPatch, states: list[str], held: Any = "held", token: Any = "token"
) -> tuple[Any, Calls]:
    """Patch the lease and tick lock and return a store whose config states are ``states`` in order."""
    calls = Calls()
    configs = iter(SimpleNamespace(state=state) for state in states)
    store = SimpleNamespace(config=_given(lambda slug: next(configs)), ensure_code=_given(lambda slug: next(configs)))

    @contextmanager
    def held_pass(store: Any, slug: str, lease_held: Any) -> Iterator[None]:
        calls.append(("held pass", (slug, lease_held)))
        yield

    monkeypatch.setattr("scripts.swarm.commands.hive_id", lambda: "hive")
    monkeypatch.setattr(lease, "acquire", lambda store, slug, hive: calls.append(("acquire", (slug, hive))) or held)
    monkeypatch.setattr(lease, "ttl_ms", lambda: 30)
    monkeypatch.setattr(lease, "held_pass", held_pass)
    monkeypatch.setattr(controller, "take_tick_lock", lambda *args: calls.append(("take", args[1:])) or token)
    monkeypatch.setattr(controller, "release_tick_lock", calls.fn("release"))
    return store, calls


@pytest.mark.feature("swarm-tick-master-placement")
@pytest.mark.parametrize("state", ["stopping", "stopped"])
def test_the_tick_lock_is_never_taken_for_a_stopping_swarm(monkeypatch: pytest.MonkeyPatch, state: str) -> None:
    store, calls = _lock(monkeypatch, [state])
    assert masters._under_tick_lock("sw", store, lambda config, held: ["acted"], "caller") == []
    assert calls == []


@pytest.mark.feature("swarm-tick-master-placement")
def test_the_tick_lock_waits_without_a_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    store, calls = _lock(monkeypatch, ["running"], held=None)
    assert masters._under_tick_lock("sw", store, lambda config, held: ["acted"], "caller") == []
    assert calls == [("acquire", ("sw", "hive"))]


@pytest.mark.feature("swarm-tick-master-placement")
def test_the_tick_lock_waits_when_another_pass_holds_it(monkeypatch: pytest.MonkeyPatch) -> None:
    store, calls = _lock(monkeypatch, ["running"], token=None)
    assert masters._under_tick_lock("sw", store, lambda config, held: ["acted"], "caller") == []
    assert calls == [("acquire", ("sw", "hive")), ("take", ("sw", "held", 30, "caller"))]


@pytest.mark.feature("swarm-tick-master-placement")
@pytest.mark.parametrize("state", ["stopping", "stopped"])
def test_the_tick_lock_releases_without_acting_once_the_swarm_stops(
    monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    store, calls = _lock(monkeypatch, ["running", state])
    assert masters._under_tick_lock("sw", store, lambda config, held: ["acted"], "caller") == []
    assert calls[-1] == ("release", (store, "sw", "token", "caller"))
    assert "held pass" not in calls.names()


@pytest.mark.feature("swarm-tick-master-placement")
def test_the_tick_lock_acts_inside_the_held_pass_and_releases(monkeypatch: pytest.MonkeyPatch) -> None:
    store, calls = _lock(monkeypatch, ["running", "running"])

    def act(config: Any, held: Any) -> list[str]:
        calls.append(("act", (config.state, held)))
        return ["acted"]

    assert masters._under_tick_lock("sw", store, act, "caller") == ["acted"]
    assert calls == [
        ("acquire", ("sw", "hive")),
        ("take", ("sw", "held", 30, "caller")),
        ("held pass", ("sw", "held")),
        ("act", ("running", "held")),
        ("release", (store, "sw", "token", "caller")),
    ]


@pytest.mark.feature("swarm-tick-master-placement")
def test_the_tick_lock_releases_when_the_act_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    store, calls = _lock(monkeypatch, ["running", "running"])

    def act(config: Any, held: Any) -> list[str]:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="^boom$"):
        masters._under_tick_lock("sw", store, act, "caller")
    assert calls[-1] == ("release", (store, "sw", "token", "caller"))
