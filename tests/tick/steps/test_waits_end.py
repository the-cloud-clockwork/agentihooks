from copy import copy
from types import SimpleNamespace
from typing import Any, NoReturn

import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm import cli, idle, pr_wait, tick, timing, waits
from scripts.swarm.store import AgentRecord
from scripts.tick import engine, pool
from scripts.tick.deadline import Deadline
from scripts.tick.sites import _GraphView
from scripts.tick.step import Context
from scripts.tick.steps import agents, report
from tests.swarm.test_ready import AGENT, MERGED, NOW, URL, merging

_fixtures = merging
pytestmark = pytest.mark.unit

BASE_S = 1_700_000_000.0
WAITS_END = "waits end"


@pytest.mark.feature("tick-merged-task-settlement-after-overrun")
def test_waits_end(merging: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    store = merging.store
    store.put_agent("sw", AgentRecord(AGENT, "eng", "t1", state="working"))
    idle.declare_wait(store.redis, "sw", AGENT, NOW + 60_000, "merge", NOW, waits.on("merge", URL))
    monkeypatch.setattr(cli, "now_ms", lambda: NOW)
    state = {
        "store": store,
        "slug": "sw",
        "rows": merging.rows,
        "inbox": InboxStore(store.redis),
        "view": {URL: MERGED}.get,
    }
    step = copy(next(step for step in report.STEPS if step.name == "waits end"))
    step.interval_s = 0.0
    with pool.running(critical_s=0.0, slow_s=1.0):
        records = engine.run_graph((step,), Context(_GraphView(state, {}), Deadline.after(5.0)))
    assert records[0].outcome == engine.OK
    assert idle.wait(store.redis, "sw", AGENT) is None
    assert merging.rows["t1"]["state"] == "pr"


@pytest.mark.feature("tick-waits-end-records-its-end-every-run")
def test_waits_end_records_its_end_on_every_run_so_a_checked_wait_holds(
    merging: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = merging.store
    clock = [BASE_S]
    monkeypatch.setattr(cli, "now_ms", lambda: int(clock[0] * 1000))
    monkeypatch.setattr(engine.time, "time", lambda: clock[0])
    state = {
        "store": store,
        "slug": "sw",
        "rows": merging.rows,
        "inbox": InboxStore(store.redis),
        "view": {URL: MERGED}.get,
    }
    step = next(step for step in report.STEPS if step.name == WAITS_END)
    checked = {"until": NOW + 60_000, "at": NOW, "on": waits.on("checks", URL)}
    waits.mark_ended(store, "sw", int(BASE_S * 1000) - 2 * tick.IDLE_KILL_MS)
    assert agents._holding_wait(store, "sw", checked, int(clock[0] * 1000)) is None
    ends: list[float] = []
    completed: list[int | None] = []
    with timing.tick("sw", store):
        for index in range(2):
            start = BASE_S + index * 60.0
            clock[0] = start + 5.0
            token = timing.PASS.set({"pass_id": f"p{index}", "start_ms": start * 1000})
            try:
                with pool.running(critical_s=0.0, slow_s=1.0):
                    records = engine.run_graph((step,), Context(_GraphView(state, {}), Deadline.after(5.0)))
            finally:
                timing.PASS.reset(token)
            assert records[0].outcome == engine.OK
            ends.append(timing.step_ends(store, "sw")[WAITS_END])
            completed.append(waits.ended_at(store, "sw"))
    last = int((BASE_S + 65.0) * 1000)
    assert ends == [BASE_S + 5.0, BASE_S + 65.0]
    assert completed == [int((BASE_S + 5.0) * 1000), last]
    assert agents._holding_wait(store, "sw", checked, last + tick.IDLE_KILL_MS - 1) == checked
    assert agents._holding_wait(store, "sw", checked, last + tick.IDLE_KILL_MS) is None


def _failing_end_pass(*_: object) -> NoReturn:
    """Raise the way a waits end pass that fails on every tick does."""
    raise RuntimeError("waits end pass failed")


@pytest.mark.feature("tick-failing-waits-end-lets-checked-waits-lapse")
def test_a_failing_waits_end_pass_leaves_the_end_checked_waits_read(
    merging: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = merging.store
    clock = [BASE_S]
    monkeypatch.setattr(cli, "now_ms", lambda: int(clock[0] * 1000))
    monkeypatch.setattr(engine.time, "time", lambda: clock[0])
    state = {
        "store": store,
        "slug": "sw",
        "rows": merging.rows,
        "inbox": InboxStore(store.redis),
        "view": {URL: MERGED}.get,
    }
    step = next(step for step in report.STEPS if step.name == WAITS_END)
    checked = {"until": NOW + 60_000, "at": NOW, "on": waits.on("checks", URL)}
    window_s = tick.IDLE_KILL_MS / 1000
    outcomes: list[str] = []
    with timing.tick("sw", store):
        for index, start in enumerate((BASE_S, BASE_S + window_s, BASE_S + window_s + 60.0)):
            if index == 1:
                monkeypatch.setattr(waits, "end_pass", _failing_end_pass)
            clock[0] = start
            token = timing.PASS.set({"pass_id": f"p{index}", "start_ms": start * 1000})
            try:
                with pool.running(critical_s=0.0, slow_s=1.0):
                    records = engine.run_graph((step,), Context(_GraphView(state, {}), Deadline.after(5.0)))
            finally:
                timing.PASS.reset(token)
            outcomes.append(records[0].outcome)
    assert outcomes == [engine.OK, engine.OK, engine.OK]
    assert agents._holding_wait(store, "sw", checked, int(clock[0] * 1000)) is None


@pytest.mark.feature("tick-waits-end-isolates-agent-failures")
@pytest.mark.parametrize("failure", ["read", "resolve", "push"])
def test_a_raising_wait_does_not_stop_other_agents_or_pass_completion(
    merging: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], failure: str
) -> None:
    store = merging.store
    bad, done, pending = (f"engineer@a1b2c3-000{index}" for index in range(1, 4))
    records = [AgentRecord(name, "eng", "t2", state="working") for name in (bad, done, pending)]
    for agent, target in zip(records, ("bad", "done", "pending"), strict=True):
        store.put_agent("sw", agent)
        idle.declare_wait(store.redis, "sw", agent.name, NOW + 60_000, "", NOW, waits.on("task", target))
    monkeypatch.setattr(store, "agents", lambda slug: records)
    read_wait = idle.wait
    resolve_wait = waits.resolution
    bad_wait = read_wait(store.redis, "sw", bad)
    pending_wait = read_wait(store.redis, "sw", pending)
    if failure == "push":
        pr_wait.mark(store.redis, "sw", bad, NOW, "the-cloud-clockwork/agentihooks", "bad", "head")
        merging.rows["t2"]["pr_url"] = URL

    def read(redis: Any, slug: str, name: str) -> Any:
        if name == bad and failure in {"read", "push"}:
            raise ValueError("broken wait record")
        return read_wait(redis, slug, name)

    def resolve(held: dict[str, Any], *args: Any) -> str:
        if held["target"] == "bad" and failure == "resolve":
            raise ValueError("broken wait record")
        return resolve_wait(held, *args)

    monkeypatch.setattr(idle, "wait", read)
    monkeypatch.setattr(waits, "resolution", resolve)
    monkeypatch.setattr(cli, "now_ms", lambda: NOW)
    inbox = InboxStore(store.redis)
    state = {
        "store": store,
        "slug": "sw",
        "rows": {**merging.rows, "done": {"id": "done", "state": "done"}, "pending": {"state": "claimed"}},
        "inbox": inbox,
        "view": merging.pulls.get,
    }
    step = copy(next(step for step in report.STEPS if step.name == WAITS_END))
    step.interval_s = 0.0
    waits.mark_ended(store, "sw", NOW - 2 * tick.IDLE_KILL_MS)
    with pool.running(critical_s=0.0, slow_s=1.0):
        runs = engine.run_graph((step,), Context(_GraphView(state, {}), Deadline.after(5.0)))

    assert runs[0].outcome == engine.OK
    assert read_wait(store.redis, "sw", done) is None
    assert read_wait(store.redis, "sw", bad) == bad_wait
    assert read_wait(store.redis, "sw", pending) == pending_wait
    assert waits.ended_at(store, "sw") == NOW
    assert agents._holding_wait(store, "sw", pending_wait, NOW) == pending_wait
    notices = inbox.inbox(done)
    assert len(notices) == 1
    assert "Your wait on task done, now done has ended." in notices[0].text
    assert inbox.inbox(bad) == []
    assert inbox.inbox(pending) == []
    errors = capsys.readouterr().err
    assert f"wait of {bad} failed: broken wait record" in errors
    if failure == "push":
        assert f"push wait of {bad} failed: broken wait record" in errors
