"""Master down, the woken check, night watch and the group runner live in the masters step module, and the swarm tick
module re-exports each one so a patched tick seam still applies."""

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.swarm import tick
from scripts.swarm.health import night_watch
from scripts.swarm.store import AgentRecord
from scripts.targets import codex_target
from scripts.tick import engine
from scripts.tick.step import Domain
from scripts.tick.steps import PASS, masters

pytestmark = pytest.mark.unit

MOVED = ("_master_down", "_woken", "_night_watch", "_PassView", "_run_group", "_print_actions", "_codex_hook_order")
SEAT = "master@sw"


@pytest.mark.feature("swarm-tick-masters-moved")
@pytest.mark.parametrize("name", MOVED)
def test_tick_reexports_the_moved_name(name: str) -> None:
    moved = getattr(masters, name)
    assert moved.__module__ == "scripts.tick.steps.masters"
    assert getattr(tick, "run_group" if name == "_run_group" else name) is moved


@pytest.mark.feature("swarm-tick-masters-moved")
def test_the_tick_module_holds_no_moved_definition() -> None:
    text = Path(str(tick.__file__)).read_text(encoding="utf-8")
    names = (*MOVED, "run_group")
    assert [name for name in names if f"\ndef {name}(" in text or f"\nclass {name}" in text] == []


@pytest.mark.feature("swarm-tick-masters-moved")
def test_the_step_wrappers_call_the_moved_operations_through_the_tick_module() -> None:
    calls: list[tuple[Any, ...]] = []

    def skip_refused(function: Any, *args: Any) -> list[str]:
        calls.append((function, *args))
        return ["ran"]

    fake = SimpleNamespace(skip_refused=skip_refused, _master_down="down", _night_watch="watch")
    state: dict[str, Any] = {
        "slug": "sw",
        "config": "config",
        "store": "store",
        "ledger": "ledger",
        "runtime": "runtime",
        "now_ms": 7,
        "doc": "doc",
        "sleeping": True,
    }
    assert masters._master_down_step(fake, state.get) == ["ran"]
    assert masters._night_watch_step(fake, state.get) == ["ran"]
    assert calls == [
        ("down", "sw", "config", "store", "ledger", "runtime", 7),
        ("watch", "sw", "store", "ledger", "runtime", "doc", 7, True),
    ]


class _Item:
    def __init__(self, item_id: str, state: str = "pending", operator: bool = True) -> None:
        self.id = item_id
        self.state = state
        self.operator = operator


class _Inbox:
    """An inbox over fixed items recording every redirect, note and cancel."""

    items: list[_Item] = []
    histories: dict[str, list[dict[str, str]]] = {}

    def __init__(self, redis: Any) -> None:
        self.redis = redis
        self.redirected: list[tuple[str, ...]] = []
        self.noted: list[tuple[Any, ...]] = []
        _Inbox.last = self

    def pending_items(self, seat: str) -> list[_Item]:
        return [item for item in self.items if item.state == "pending"] if seat == SEAT else []

    def inbox(self, seat: str) -> list[_Item]:
        return list(self.items) if seat == SEAT else []

    def history(self, item_id: str) -> list[dict[str, str]]:
        return self.histories.get(item_id, [])

    def redirect(self, item_id: str, by: str, seat: str, reason: str) -> None:
        self.redirected.append((item_id, by, seat, reason))

    def note(self, item_id: str, event: str, by: str, text: str, now_ms: int) -> None:
        self.noted.append((item_id, event, by, text, now_ms))


class _Ledger:
    def __init__(self, closed: bool = False) -> None:
        self.is_closed = closed
        self.notified: list[tuple[str, str]] = []

    def closed(self, slug: str) -> bool:
        return self.is_closed and slug == "sw"

    def notify(self, slug: str, text: str) -> None:
        self.notified.append((slug, text))


@pytest.fixture
def inbox(monkeypatch: pytest.MonkeyPatch) -> type[_Inbox]:
    monkeypatch.setattr(tick, "InboxStore", _Inbox)
    monkeypatch.setattr(tick, "_operator_line", lambda item: item.operator)
    monkeypatch.setattr(_Inbox, "items", [])
    monkeypatch.setattr(_Inbox, "histories", {})
    return _Inbox


def _store(agents: list[AgentRecord] | None = None) -> SimpleNamespace:
    return SimpleNamespace(redis="redis", agents=lambda slug: list(agents or []) if slug == "sw" else [])


@pytest.mark.feature("swarm-tick-masters-moved")
def test_woken_reads_pending_operator_lines_at_the_master_seat(
    inbox: type[_Inbox], monkeypatch: pytest.MonkeyPatch
) -> None:
    cancelled: list[tuple[Any, str]] = []
    monkeypatch.setattr(
        tick, "priming", SimpleNamespace(TEMPLATE="doctor", cancel_master_items=lambda i, s: cancelled.append((i, s)))
    )
    config = SimpleNamespace(template="plain")
    assert masters._woken("sw", config, _store(), _Ledger()) is False
    inbox.items = [_Item("a", operator=False)]
    assert masters._woken("sw", config, _store(), _Ledger()) is False
    inbox.items = [_Item("a", operator=False), _Item("b")]
    assert masters._woken("sw", config, _store(), _Ledger()) is True
    assert masters._woken("sw", config, _store(), _Ledger(closed=True)) is True
    assert masters._woken("sw", SimpleNamespace(template="doctor"), _store(), _Ledger()) is True
    assert cancelled == []
    assert masters._woken("sw", SimpleNamespace(template="doctor"), _store(), _Ledger(closed=True)) is False
    assert cancelled == [(inbox.last, "sw")]
    inbox.items = []
    assert masters._woken("sw", SimpleNamespace(template="doctor"), _store(), _Ledger(closed=True)) is False
    assert len(cancelled) == 1


def _master(name: str, started_at: int, state: str = "working", lane: str = "master") -> AgentRecord:
    return AgentRecord(name, lane, "master", started_at=started_at, state=state)


def _runtime(*live: str) -> SimpleNamespace:
    return SimpleNamespace(live_names=lambda: set(live))


NOW = 10 * tick.STARTUP_GRACE_MS


@pytest.mark.feature("swarm-tick-masters-moved")
@pytest.mark.parametrize(
    ("agents", "live", "state"),
    [
        ([_master("m1", 0)], ("m1",), "running"),
        ([_master("m1", NOW - tick.STARTUP_GRACE_MS)], (), "running"),
        ([_master("m1", 0)], (), "stopping"),
    ],
)
def test_master_down_stays_quiet_while_a_master_lives_starts_or_the_swarm_stops(
    inbox: type[_Inbox], agents: list[AgentRecord], live: tuple[str, ...], state: str
) -> None:
    inbox.items = [_Item("a")]
    ledger = _Ledger()
    found = masters._master_down("sw", SimpleNamespace(state=state), _store(agents), ledger, _runtime(*live), NOW)
    assert found == []
    assert ledger.notified == []


@pytest.mark.feature("swarm-tick-masters-moved")
def test_master_down_tells_each_waiting_operator_line_once_and_returns_taken_lines(inbox: type[_Inbox]) -> None:
    agents = [
        _master("m1", NOW - tick.STARTUP_GRACE_MS - 1),
        _master("m2", NOW, state="finished"),
        _master("e1", NOW, lane="eng"),
    ]
    inbox.items = [
        _Item("new"),
        _Item("taken", state="delivered"),
        _Item("told", state="delivered"),
        _Item("plain", operator=False),
        *[_Item(state, state=state) for state in tick.CLOSED],
    ]
    inbox.histories = {"told": [{"event": tick.DOWN_TOLD}], "new": [{"event": "sent"}]}
    ledger = _Ledger()
    found = masters._master_down("sw", SimpleNamespace(state="running"), _store(agents), ledger, _runtime("e1"), NOW)
    assert found == ["master down, told the operator about 2 waiting lines"]
    assert inbox.last.redirected == [
        ("taken", "swarm", SEAT, tick.REDELIVERED),
        ("told", "swarm", SEAT, tick.REDELIVERED),
    ]
    assert inbox.last.noted == [
        ("new", tick.DOWN_TOLD, "swarm", tick.MASTER_DOWN, NOW),
        ("taken", tick.DOWN_TOLD, "swarm", tick.MASTER_DOWN, NOW),
    ]
    assert ledger.notified == [("sw", tick.MASTER_DOWN)]


@pytest.mark.feature("swarm-tick-masters-moved")
def test_master_down_says_nothing_when_every_line_was_told(inbox: type[_Inbox]) -> None:
    inbox.items = [_Item("told")]
    inbox.histories = {"told": [{"event": tick.DOWN_TOLD}]}
    ledger = _Ledger()
    found = masters._master_down("sw", SimpleNamespace(state="running"), _store(), ledger, _runtime(), NOW)
    assert found == []
    assert inbox.last.noted == []
    assert ledger.notified == []


class _WatchStore:
    def __init__(self) -> None:
        self.redis = SimpleNamespace(deleted=[], delete=lambda key: self.redis.deleted.append(key))

    def key(self, slug: str, name: str) -> str:
        return f"{slug}:{name}"

    def config(self, slug: str) -> str:
        return f"config of {slug}"


@pytest.mark.feature("swarm-tick-masters-moved")
@pytest.mark.parametrize(("alerted", "deleted"), [(True, ["sw:master-start"]), (False, [])])
def test_night_watch_runs_the_watch_with_a_respawn_that_places_the_master_past_the_host(
    monkeypatch: pytest.MonkeyPatch, alerted: bool, deleted: list[str]
) -> None:
    store = _WatchStore()
    started: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        tick, "master_start", SimpleNamespace(read=lambda s, slug: {"alerted": alerted} if slug == "sw" else {})
    )
    monkeypatch.setattr(tick, "PLACING", nullcontext())

    def master(*args: Any, **kwargs: Any) -> list[str]:
        started.append((*args, kwargs))
        return ["spawned"]

    monkeypatch.setattr(tick, "_master", master)
    monkeypatch.setattr(night_watch, "Watch", lambda *args: args)
    monkeypatch.setattr(night_watch, "run", lambda watch: ["watched", watch])
    found = masters._night_watch("sw", store, "ledger", "runtime", "doc", 7, True)
    assert found[0] == "watched"
    *fields, respawn = found[1]
    assert fields == [store, "sw", "ledger", "runtime", "doc", 7, True]
    assert respawn() == ["spawned"]
    assert store.redis.deleted == deleted
    assert started == [("sw", "config of sw", store, "runtime", 7, {"past_host": True})]


@pytest.mark.feature("swarm-tick-masters-moved")
def test_the_pass_view_reads_only_the_pass_domain() -> None:
    view = masters._PassView({"slug": "sw"})
    assert view.read(PASS, "slug") == "sw"
    assert view.read(PASS, "doc") is None
    assert view.read(Domain("other"), "slug") is None


@pytest.mark.feature("swarm-tick-masters-moved")
@pytest.mark.parametrize(("state", "box"), [({"box": {"raised": 1}}, {"raised": 1}), ({}, {})])
def test_run_group_runs_the_registered_steps_and_notes_the_left_box(
    monkeypatch: pytest.MonkeyPatch, state: dict[str, Any], box: dict[str, Any]
) -> None:
    seen: dict[str, Any] = {}
    record = engine.StepRecord("one", 0.0, 1.0)
    monkeypatch.setattr(tick, "GROUP_BUDGET_S", 12.5)
    monkeypatch.setattr(tick, "Deadline", SimpleNamespace(after=lambda budget: ("deadline", budget)))
    monkeypatch.setattr(tick, "Context", lambda view, deadline: ("ctx", view, deadline))
    monkeypatch.setattr(tick, "task_steps", SimpleNamespace(appliers=lambda s: ("appliers", s)))
    monkeypatch.setattr(
        tick,
        "steps",
        SimpleNamespace(
            note_left=lambda b, records: seen.update(left=(b, records)),
            actions=lambda records: [f"acted {len(records)}"],
        ),
    )
    printed: list[tuple[Any, ...]] = []
    monkeypatch.setattr(tick, "_print_actions", lambda b, records: printed.append((b, records)))

    def run_group(name: str, ctx: Any, *, apply: Any, finished: Any) -> tuple[engine.StepRecord, ...]:
        seen.update(name=name, ctx=ctx, apply=apply)
        finished(("late",))
        return (record,)

    monkeypatch.setattr(engine, "run_group", run_group)
    found = masters._run_group("masters", **state)
    assert found == ["acted 1"]
    assert seen["name"] == "masters"
    label, view, deadline = seen["ctx"]
    assert (label, deadline) == ("ctx", ("deadline", 12.5))
    assert view.read(PASS, "box") == state.get("box")
    assert seen["apply"] == ("appliers", state)
    assert seen["left"] == (box, (record,))
    assert printed == [(box, ("late",))]


@pytest.mark.feature("swarm-tick-masters-moved")
def test_print_actions_prints_actions_failures_and_the_held_exception(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(tick, "steps", SimpleNamespace(actions=lambda records: ["did a", "did b"]))
    records = (engine.StepRecord("fine", 0.0, 1.0), engine.StepRecord("bad", 0.0, 1.0, error="boom"))
    masters._print_actions({"raised": ValueError("x")}, records)
    assert capsys.readouterr().out == (
        "did a\ndid b\nslow lane step bad failed: boom\na slow lane step held ValueError('x')\n"
    )
    masters._print_actions({}, (engine.StepRecord("fine", 0.0, 1.0),))
    assert capsys.readouterr().out == "did a\ndid b\n"


@pytest.mark.feature("swarm-tick-masters-moved")
@pytest.mark.parametrize(("moved", "found"), [([], []), (["a", "b"], ["{path}: a, b"])])
def test_codex_hook_order_reports_the_hooks_it_restored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, moved: list[str], found: list[str]
) -> None:
    homes: list[Path] = []
    monkeypatch.setattr(codex_target, "codex_home", lambda: tmp_path)
    monkeypatch.setattr(codex_target, "restore_hook_order", lambda home: homes.append(home) or moved)
    path = (tmp_path / "hooks.json").resolve()
    expected = [f"restored the approved Codex hook order in {line.format(path=path)}" for line in found]
    assert masters._codex_hook_order() == expected
    assert homes == [tmp_path]
