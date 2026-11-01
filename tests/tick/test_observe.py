"""The start and observation steps run the tick's hand written start and observe calls from the registry."""

from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.swarm import automated_gc
from scripts.swarm import tick as tick_module
from scripts.tick import engine
from scripts.tick.deadline import Deadline
from scripts.tick.step import Context, Domain
from scripts.tick.steps import PASS, actions, observe

pytestmark = pytest.mark.unit


class _State:
    def __init__(self, state: dict[str, object]) -> None:
        self.state = state

    def read(self, domain: Domain, key: str) -> object | None:
        return self.state.get(key) if domain == PASS else None


class _Redis:
    def __init__(self, keys: dict[str, object]) -> None:
        self.keys = keys

    def get(self, key: str) -> object | None:
        return self.keys.get(key)

    def exists(self, key: str) -> bool:
        return key in self.keys

    def set(self, key: str, value: object) -> None:
        self.keys[key] = value


class _Store:
    def __init__(self, state: str, keys: dict[str, object] | None = None) -> None:
        self.redis = _Redis(keys or {})
        self.state = state
        self.updates: list[dict[str, object]] = []
        self.execution_registry = SimpleNamespace(reconcile=None)

    def ensure_code(self, slug: str) -> SimpleNamespace:
        return SimpleNamespace(slug=slug, state=self.state, engine="tick")

    def update(self, slug: str, **fields: object) -> SimpleNamespace:
        self.updates.append(fields)
        return SimpleNamespace(slug=slug, state=fields["state"], engine="tick")

    def key(self, slug: str, name: str) -> str:
        return f"{slug}:{name}"


class _Ledger:
    def __init__(self) -> None:
        self.reads = 0

    def state(self, slug: str) -> dict[str, object]:
        self.reads += 1
        return {"tasks": [{"id": "t1"}], "slug": slug}


def _state(store: _Store) -> tuple[dict[str, object], dict[str, Any]]:
    box: dict[str, Any] = {}
    return {
        "slug": "sw",
        "store": store,
        "ledger": _Ledger(),
        "runtime": "runtime",
        "now_ms": 7,
        "box": box,
    }, box


def _ctx(state: dict[str, object]) -> Context:
    return Context(_State(state), Deadline.after(60.0))


def _fake(seen: list[tuple[str, tuple[Any, ...]]], name: str, result: list[str] | None) -> Any:
    def call(*args: Any) -> list[str] | None:
        seen.append((name, args))
        return result

    call.__qualname__ = name
    return call


def _observe_fakes(monkeypatch: pytest.MonkeyPatch, store: _Store) -> list[tuple[str, tuple[Any, ...]]]:
    seen: list[tuple[str, tuple[Any, ...]]] = []
    monkeypatch.setattr(tick_module.pane_evidence, "sweep", _fake(seen, "panes", ["pane"]))
    store.execution_registry.reconcile = _fake(seen, "reconcile", ["reconciled"])
    monkeypatch.setattr(tick_module.master_move, "fail_over", _fake(seen, "fail over", ["moved"]))
    monkeypatch.setattr(tick_module, "_recover_master", _fake(seen, "recover", ["recovered"]))
    monkeypatch.setattr(tick_module.freeze, "watched", lambda slug, store, ledger, doc: {**doc, "watched": True})
    monkeypatch.setattr(automated_gc, "run", _fake(seen, "cleanup", ["cleaned"]))
    monkeypatch.setattr(tick_module, "_spawn", _fake(seen, "spawn", ["spawned"]))
    monkeypatch.setattr(tick_module.exits, "sweep", _fake(seen, "exits", ["exited"]))
    monkeypatch.setattr(tick_module.master_start, "observe", _fake(seen, "start watch", ["watched"]))
    monkeypatch.setattr(tick_module, "_launch_checks", _fake(seen, "launch checks", ["checked"]))
    monkeypatch.setattr(tick_module.resume, "track_outages", _fake(seen, "outages", None))
    monkeypatch.setattr(tick_module, "_verify", _fake(seen, "verify", ["verified"]))
    monkeypatch.setattr(tick_module.lifetime, "sleeping", lambda slug, store, rows: False)

    @contextmanager
    def placement(runtime: object) -> Any:
        seen.append(("placement", (runtime,)))
        yield

    monkeypatch.setattr(tick_module, "_placement_pass", placement)
    return seen


def test_the_start_and_observe_steps_sit_in_their_groups_in_legacy_order() -> None:
    assert [step.name for step in engine.group_steps("start")] == ["config load", "stopped branch"]
    assert [step.name for step in engine.group_steps("observe")] == [
        "pane evidence",
        "execution reconcile",
        "master move fail over",
        "recover master",
        "ledger state",
        "automated cleanup",
        "spawn due",
        "exits sweep",
        "master start watch",
        "launch checks",
        "track outages",
        "verify",
    ]
    assert len(observe.STEPS) == 14


def test_a_running_swarm_loads_its_config_and_skips_the_stopped_branch() -> None:
    store = _Store("running")
    state, box = _state(store)
    assert actions(engine.run_group("start", _ctx(state))) == []
    assert box == {"config": SimpleNamespace(slug="sw", state="running", engine="tick")}


def test_a_stopped_swarm_that_sleeps_finishes_the_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, tuple[Any, ...]]] = []
    store = _Store("stopped")
    state, box = _state(store)
    monkeypatch.setattr(automated_gc, "run", _fake(seen, "cleanup", ["cleaned"]))
    monkeypatch.setattr(tick_module, "_woken", lambda slug, config, store, ledger: False)
    monkeypatch.setattr(tick_module, "_close_space", _fake(seen, "close", ["closed"]))
    monkeypatch.setattr(tick_module, "_bin_closed", _fake(seen, "bin", ["binned"]))
    assert actions(engine.run_group("start", _ctx(state))) == ["cleaned"]
    assert box["finished"] == ["closed", "binned"]
    assert [name for name, _ in seen] == ["cleanup", "close", "bin"]
    assert seen[0][1][:5] == ("sw", store, state["ledger"], "runtime", 7)
    assert seen[0][1][5].args == ("sw", store, state["ledger"], {"t1": {"id": "t1"}})
    assert seen[2][1] == ("sw", store, state["ledger"], {"tasks": [{"id": "t1"}], "slug": "sw"})
    assert store.updates == []


def test_a_stopped_swarm_with_retired_tasks_awake_keeps_finishing(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _Store("stopped", {"sw:master-retired-tasks": "1"})
    state, box = _state(store)
    monkeypatch.setattr(automated_gc, "run", lambda *args: [])
    monkeypatch.setattr(tick_module, "_woken", lambda slug, config, store, ledger: False)
    monkeypatch.setattr(tick_module.lifetime, "sleeping", lambda slug, store, rows: True)
    monkeypatch.setattr(tick_module, "_close_space", lambda *args: [])
    monkeypatch.setattr(tick_module, "_bin_closed", lambda *args: ["binned"])
    assert actions(engine.run_group("start", _ctx(state))) == []
    assert box["finished"] == ["binned"]


def test_a_stopped_swarm_the_operator_wrote_to_pauses_to_start_the_master(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _Store("stopped", {"sw:master-retired-tasks": "1"})
    state, box = _state(store)
    monkeypatch.setattr(automated_gc, "run", lambda *args: ["cleaned"])
    monkeypatch.setattr(tick_module, "_woken", lambda slug, config, store, ledger: False)
    monkeypatch.setattr(tick_module.lifetime, "sleeping", lambda slug, store, rows: False)
    assert actions(engine.run_group("start", _ctx(state))) == [
        "cleaned",
        "the operator wrote on the ledger, paused to start the master",
    ]
    assert "finished" not in box
    assert store.updates == [{"state": "paused"}]
    assert box["config"].state == "paused"


def test_the_observe_group_runs_its_calls_with_the_pass_state(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _Store("running", {"sw:spawn-due": 1})
    seen = _observe_fakes(monkeypatch, store)
    state, box = _state(store)
    box["config"] = config = SimpleNamespace(state="running", engine="tick")
    found = actions(engine.run_group("observe", _ctx(state)))
    assert found == [
        "pane",
        "reconciled",
        "moved",
        "recovered",
        "cleaned",
        "spawned",
        "exited",
        "watched",
        "checked",
        "verified",
    ]
    rows = {"t1": {"id": "t1"}}
    doc = {"tasks": [{"id": "t1"}], "slug": "sw", "watched": True}
    assert (box["doc"], box["rows"]) == (doc, rows)
    ledger = state["ledger"]
    names = [name for name, _ in seen]
    assert names == [
        "panes",
        "reconcile",
        "fail over",
        "recover",
        "cleanup",
        "placement",
        "spawn",
        "exits",
        "start watch",
        "launch checks",
        "outages",
        "verify",
    ]
    calls = dict(seen)
    assert calls["panes"] == ("sw", store, "runtime", 7)
    assert calls["reconcile"] == ("sw", "runtime", 7)
    assert calls["fail over"] == ("sw", config, store, "runtime", 7)
    assert calls["recover"] == ("sw", config, store, "runtime", 7)
    assert calls["cleanup"][:5] == ("sw", store, ledger, "runtime", 7)
    assert calls["cleanup"][5].args == ("sw", store, ledger, rows)
    assert calls["spawn"] == ("sw", config, store, ledger, "runtime", rows, doc, 7)
    assert calls["exits"][1:3] == ("sw", store)
    assert calls["exits"][0].redis is store.redis
    assert calls["exits"][3]() == rows
    assert calls["start watch"] == ("sw", config, store, ledger, "runtime", 7)
    assert calls["launch checks"] == ("sw", store, ledger, "runtime", rows, doc, 7)
    assert calls["outages"] == ("sw", store, "runtime", rows, 7)
    assert calls["verify"] == ("sw", store, ledger, "runtime", rows, 7)
    assert store.redis.keys["sw:spawn-due"] == 7


@pytest.mark.parametrize(
    ("state", "engine_name", "sleeping", "keys", "spawned", "due"),
    [
        ("running", "tick", False, {}, False, 7),
        ("paused", "tick", False, {"sw:spawn-due": 1}, False, 1),
        ("running", tick_module.EVENTS_ENGINE, False, {"sw:spawn-due": 1}, False, 1),
        ("running", "tick", True, {"sw:spawn-due": 1}, False, 1),
    ],
)
def test_the_spawn_due_step_spawns_only_when_due_on_a_running_tick_swarm(
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    engine_name: str,
    sleeping: bool,
    keys: dict[str, object],
    spawned: bool,
    due: object,
) -> None:
    store = _Store(state, dict(keys))
    seen = _observe_fakes(monkeypatch, store)
    monkeypatch.setattr(tick_module.lifetime, "sleeping", lambda slug, store, rows: sleeping)
    pass_state, box = _state(store)
    box.update(config=SimpleNamespace(state=state, engine=engine_name), doc={}, rows={})
    step = engine.group_steps("observe")[6]
    assert step.run(_ctx(pass_state)).writes[0].value == []
    assert ("spawn" in [name for name, _ in seen]) is spawned
    assert store.redis.keys.get("sw:spawn-due") == due


def test_a_step_that_raises_holds_its_exception_and_stops_the_later_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _Store("running")
    seen = _observe_fakes(monkeypatch, store)
    state, box = _state(store)
    box["config"] = SimpleNamespace(state="running", engine="tick")

    def broken(slug: str) -> None:
        raise ConnectionError(slug)

    state["ledger"].state = broken
    assert actions(engine.run_group("observe", _ctx(state))) == ["pane", "reconciled", "moved", "recovered"]
    assert [name for name, _ in seen] == ["panes", "reconcile", "fail over", "recover"]
    with pytest.raises(ConnectionError, match="^sw$"):
        observe.raise_held(box)


def test_raise_held_passes_when_no_step_raised() -> None:
    assert observe.raise_held({"config": 1}) is None
    assert observe.RAISED == "raised"


def test_a_failed_config_load_skips_the_stopped_branch() -> None:
    state, box = _state(_Store("stopped"))
    state["store"] = SimpleNamespace(ensure_code=lambda slug: (_ for _ in ()).throw(SystemExit(3)))
    assert actions(engine.run_group("start", _ctx(state))) == []
    assert list(box) == [observe.RAISED]
    with pytest.raises(SystemExit):
        observe.raise_held(box)
