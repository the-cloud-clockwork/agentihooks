"""Tick idle pass tests: idle counts, quiet nudges, declared waits and idle retirement."""

import pytest

from scripts.swarm.store import MASTER, AgentRecord, RedisStore
from scripts.swarm.tick import tick
from tests.swarm import test_tick
from tests.swarm.test_tick import IDLER, STALE_MS, FakeLedger, FakeRuntime, idle_for, stale_nudges, tasks, workers
from tests.swarm.test_tick_verification import seated

store = test_tick.store


def spawned(
    store: RedisStore, status: str = "idle", runtime: FakeRuntime | None = None
) -> tuple[FakeLedger, FakeRuntime]:
    """An engineer on t1 seated through hub admission, then a tick at 1_000 that seats the master, the swarm paused
    and the engineer's pane reading ``status``."""
    ledger, runtime = tasks(("t1", "eng")), runtime or FakeRuntime()
    seated(store, ledger, runtime, "t1")
    tick("sw", store, ledger, runtime, now_ms=1_000)
    store.update("sw", state="paused")
    runtime.statuses[IDLER] = status
    return ledger, runtime


@pytest.mark.feature("swarm-tick-idle-retire")
def test_an_idle_agent_is_retired_without_a_typed_nudge_and_its_task_reopened(store):
    from scripts.swarm.tick import IDLE_KILL_TICKS

    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    seated(store, ledger, runtime, "t1")
    tick("sw", store, ledger, runtime, now_ms=1_000)
    runtime.statuses["engineer@a1b2c3-0001"] = "idle"
    store.update("sw", state="paused")
    for n in range(IDLE_KILL_TICKS):
        tick("sw", store, ledger, runtime, now_ms=2_000 + n * 60_000)
    assert runtime.nudged == []
    assert runtime.killed == ["engineer@a1b2c3-0001"] and ledger.rows["t1"]["state"] == "open"


def test_an_agent_quiet_past_ten_minutes_gets_one_inbox_nudge_per_window_and_no_typed_prompt(store):
    ledger, runtime = spawned(store)
    tick("sw", store, ledger, runtime, now_ms=1_000 + STALE_MS)
    assert stale_nudges(store) == []
    actions = tick("sw", store, ledger, runtime, now_ms=1_001 + STALE_MS)
    [item] = stale_nudges(store)
    assert f"nudged stale {IDLER} through its inbox as message {item.id}" in actions
    assert (item.sender, item.task, item.state) == ("swarm", "t1", "pending")
    tick("sw", store, ledger, runtime, now_ms=2_000 + STALE_MS)
    assert len(stale_nudges(store)) == 1
    tick("sw", store, ledger, runtime, now_ms=1_001 + 2 * STALE_MS)
    assert len(stale_nudges(store)) == 2 and runtime.nudged == []


def test_a_pane_reading_working_is_nudged_once_its_heartbeat_is_ten_minutes_old(store):
    from scripts.swarm import idle

    ledger, runtime = spawned(store, status="working")
    idle.beat(store.redis, "sw", IDLER, "working", 1_000 + STALE_MS)
    tick("sw", store, ledger, runtime, now_ms=1_000 + 2 * STALE_MS)
    assert stale_nudges(store) == []
    tick("sw", store, ledger, runtime, now_ms=1_001 + 2 * STALE_MS)
    assert len(stale_nudges(store)) == 1 and runtime.nudged == []


def test_a_named_wait_keeps_a_quiet_agent_from_being_nudged(store):
    from scripts.swarm import idle

    ledger, runtime = spawned(store)
    idle.declare_wait(store.redis, "sw", IDLER, 1_000 + 3 * STALE_MS, "checks on the pull request", 1_000)
    tick("sw", store, ledger, runtime, now_ms=1_000 + 2 * STALE_MS)
    assert stale_nudges(store) == []


class ProviderErrorRuntime(FakeRuntime):
    def observe(self, agent):
        from scripts.swarm.pane import PaneObservation

        return PaneObservation(self.status(agent), provider_error="model at capacity", provider_checked=True)


def test_a_pane_showing_a_provider_error_is_nudged_at_once(store):
    ledger, runtime = spawned(store, status="working", runtime=ProviderErrorRuntime())
    tick("sw", store, ledger, runtime, now_ms=2_000)
    [item] = stale_nudges(store)
    assert "provider error" in item.text and runtime.nudged == []


def test_the_master_is_never_nudged_for_being_quiet(store):
    ledger, runtime = spawned(store)
    [master] = [a for a in store.agents("sw") if a.lane == MASTER]
    runtime.statuses[master.name] = "idle"
    tick("sw", store, ledger, runtime, now_ms=1_001 + 2 * STALE_MS)
    assert stale_nudges(store, master.name) == [] and len(stale_nudges(store)) == 1


def test_an_open_swarm_outage_leaves_the_quiet_agent_to_provider_recovery(store, monkeypatch):
    from scripts.swarm import outage

    ledger, runtime = spawned(store)
    record = {"kind": "provider", "started_at": 1_000, "ended_at": None, "evidence": ["provider down"], "past": []}
    monkeypatch.setattr(outage, "open_records", lambda slug: [record])
    tick("sw", store, ledger, runtime, now_ms=1_001 + 2 * STALE_MS)
    assert stale_nudges(store) == [] and runtime.nudged == []


@pytest.mark.parametrize(("pending", "expected"), [(True, 0), (False, 1)])
def test_a_pending_recovery_leaves_the_quiet_agent_to_provider_recovery(store, pending, expected):
    from scripts.swarm.pane import PaneObservation
    from scripts.swarm.tick import _stale_nudge

    agent = AgentRecord(IDLER, "eng", "t1", started_at=1_000)
    actions = _stale_nudge("sw", store, agent, PaneObservation("idle"), pending, 1_001 + STALE_MS)
    assert len(actions) == len(stale_nudges(store)) == expected


def test_a_pending_recovery_blocks_the_nudge_during_the_tick(store, monkeypatch):
    from scripts.swarm import resume

    ledger, runtime = spawned(store)
    monkeypatch.setattr(resume, "recovery_pending", lambda *args: True)
    monkeypatch.setattr(resume, "recover_outages", lambda *args: [])
    tick("sw", store, ledger, runtime, now_ms=1_001 + STALE_MS)
    assert stale_nudges(store) == []


def test_the_last_idle_tick_before_retirement_still_nudges(store):
    from dataclasses import replace

    from scripts.swarm.tick import IDLE_KILL_TICKS

    ledger, runtime = spawned(store)
    agent = store.agents("sw")[0]
    store.put_agent("sw", replace(agent, idle_ticks=IDLE_KILL_TICKS - 2))
    tick("sw", store, ledger, runtime, now_ms=1_001 + STALE_MS)
    assert len(stale_nudges(store)) == 1
    assert runtime.killed == []


def test_a_nudge_the_routed_runtime_refuses_answers_with_the_router_reason(store):
    from scripts.swarm.tick import NUDGE
    from scripts.swarm_v2.runtime.base import Capability
    from scripts.swarm_v2.runtime.local import LocalHerdrRuntime

    herdr = FakeRuntime()
    runtime = routed_tick_runtime(herdr, LocalHerdrRuntime.capabilities - {Capability.COMMAND})
    agent = AgentRecord(IDLER, "eng", "t1", pane_id="w1:p1", started_at=1_000)
    assert runtime.nudge(agent, NUDGE.format(slug="sw")) == "local lacks command" and herdr.nudged == []


def routed_tick_runtime(herdr, capabilities=None):
    from scripts.swarm_v2.runtime.base import RuntimeRouter
    from scripts.swarm_v2.runtime.local import LocalHerdrRuntime
    from scripts.swarm_v2.runtime.routed import RoutedRuntime

    local = LocalHerdrRuntime(herdr)
    local.capabilities = local.capabilities if capabilities is None else capabilities
    return RoutedRuntime(herdr, RuntimeRouter([local]))


def failing_herdr(args):
    raise RuntimeError("agent_not_found")


@pytest.mark.parametrize(("call", "answer"), [(lambda args: {}, None), (failing_herdr, "agent_not_found")])
def test_a_local_herdr_nudge_answers_by_whether_herdr_delivered_it(store, tmp_path, call, answer):
    from scripts.swarm.runtime import HerdrRuntime
    from scripts.swarm.tick import NUDGE

    herdr = FakeRuntime()
    herdr.nudge = HerdrRuntime(home=tmp_path, herdr=call).nudge
    agent = AgentRecord(IDLER, "eng", "t1", pane_id="w1:p1", started_at=1_000)
    assert routed_tick_runtime(herdr).nudge(agent, NUDGE.format(slug="sw")) == answer


def test_the_nudge_is_skipped_while_the_pane_holds_typed_input(store):
    ledger, runtime = spawned(store)
    runtime.typed[IDLER] = "wait, before you"
    tick("sw", store, ledger, runtime, now_ms=1_001 + STALE_MS)
    assert stale_nudges(store) == [] and workers(store)[0].idle_ticks == 0
    runtime.typed.clear()
    tick("sw", store, ledger, runtime, now_ms=1_002 + STALE_MS)
    assert len(stale_nudges(store)) == 1 and runtime.nudged == []


def test_the_nudge_is_skipped_inside_the_quiet_window_after_an_operator_prompt(store):
    from scripts.inbox import wake
    from scripts.swarm import idle

    ledger, runtime = spawned(store)
    quiet = wake.DEFAULT_QUIET_S * 1000
    idle.prompted(store.redis, "sw", IDLER, 1_001 + STALE_MS - quiet + 10)
    tick("sw", store, ledger, runtime, now_ms=1_001 + STALE_MS)
    assert stale_nudges(store) == []
    tick("sw", store, ledger, runtime, now_ms=1_001 + STALE_MS + 10)
    assert len(stale_nudges(store)) == 1 and runtime.nudged == []


def test_the_nudge_says_to_answer_with_the_swarm_commands_never_in_the_terminal():
    from scripts.swarm.tick import NUDGE

    assert "agentihooks msg reply" in NUDGE and "never as text in this terminal" in NUDGE


def test_each_idle_tick_is_recorded_in_the_gate_log_with_its_time_and_task(store):
    store.update("sw", autonomy="assist")
    from scripts.gates import log

    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    seated(store, ledger, runtime, "t1")
    tick("sw", store, ledger, runtime, now_ms=1_000)
    store.update("sw", state="paused")
    tick("sw", store, ledger, runtime, now_ms=1_500)
    assert log.recent("sw") == []
    runtime.statuses["engineer@a1b2c3-0001"] = "idle"
    tick("sw", store, ledger, runtime, now_ms=2_000)
    tick("sw", store, ledger, runtime, now_ms=62_000)
    rows = [(r["gate"], r["kind"], r["agent"], r["task"], r["reason"], r["at"]) for r in log.recent("sw")]
    assert rows == [
        ("idle-ticks", "count", "engineer@a1b2c3-0001", "t1", "idle tick 1", 2_000),
        ("idle-ticks", "count", "engineer@a1b2c3-0001", "t1", "idle tick 2", 62_000),
    ]


@pytest.mark.parametrize("state", ["pending", "delivered", "read"])
def test_a_retired_stalled_agent_leaves_its_items_on_its_seat(store, state):
    from scripts.inbox.store import InboxStore
    from scripts.swarm.tick import IDLE_KILL_TICKS

    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    seated(store, ledger, runtime, "t1")
    tick("sw", store, ledger, runtime, now_ms=1_000)
    inbox = InboxStore(store.redis)
    item = inbox.send("ci@a1b2c3-0009", "engineer@a1b2c3-0001", "contract confirmed")
    if state != "pending":
        getattr(inbox, "deliver" if state == "delivered" else "read")(item.id, "engineer@a1b2c3-0001")
    runtime.statuses["engineer@a1b2c3-0001"] = "idle"
    store.update("sw", state="paused")
    for n in range(IDLE_KILL_TICKS):
        tick("sw", store, ledger, runtime, now_ms=2_000 + n * 60_000)
    assert runtime.killed == ["engineer@a1b2c3-0001"]
    assert (inbox.get(item.id).address, inbox.get(item.id).state) == ("eng-1@sw", "pending")


def test_a_busy_turn_resets_the_idle_count(store):
    from scripts.swarm.tick import IDLE_NUDGE_TICKS

    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    seated(store, ledger, runtime, "t1")
    tick("sw", store, ledger, runtime, now_ms=1_000)
    for n, status in enumerate(["idle"] * (IDLE_NUDGE_TICKS - 1) + ["working"] + ["idle"] * (IDLE_NUDGE_TICKS - 1)):
        runtime.statuses["engineer@a1b2c3-0001"] = status
        tick("sw", store, ledger, runtime, now_ms=2_000 + n * 60_000)
    assert workers(store)[0].idle_ticks == IDLE_NUDGE_TICKS - 1


def test_an_idle_agent_under_a_declared_wait_is_never_nudged_or_retired(store):
    from scripts.swarm import idle
    from scripts.swarm.tick import IDLE_KILL_TICKS

    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    seated(store, ledger, runtime, "t1")
    tick("sw", store, ledger, runtime, now_ms=1_000)
    store.update("sw", state="paused")
    runtime.statuses["engineer@a1b2c3-0001"] = "idle"
    idle.declare_wait(
        store.redis, "sw", "engineer@a1b2c3-0001", 1_000 + 15 * 60_000 + IDLE_KILL_TICKS * 60_000, "deploy", 1_000
    )
    idle_for(store, ledger, runtime, 15 + IDLE_KILL_TICKS, start=2_000)
    assert runtime.nudged == [] and runtime.killed == [] and ledger.rows["t1"]["state"] == "claimed"


def test_a_wait_that_ended_lets_the_idle_count_run_again(store):
    from scripts.swarm import idle
    from scripts.swarm.tick import IDLE_KILL_TICKS

    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    seated(store, ledger, runtime, "t1")
    tick("sw", store, ledger, runtime, now_ms=1_000)
    store.update("sw", state="paused")
    runtime.statuses["engineer@a1b2c3-0001"] = "idle"
    idle.declare_wait(store.redis, "sw", "engineer@a1b2c3-0001", 2_000, "deploy", 1_000)
    store.redis.persist(idle.key("sw", "wait", "engineer@a1b2c3-0001"))
    idle_for(store, ledger, runtime, IDLE_KILL_TICKS, start=3_000)
    assert runtime.killed == ["engineer@a1b2c3-0001"]


def test_an_agent_whose_heartbeat_says_working_is_never_retired_while_its_pane_reads_idle(store):
    from scripts.swarm import idle
    from scripts.swarm.tick import IDLE_KILL_TICKS

    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    seated(store, ledger, runtime, "t1")
    tick("sw", store, ledger, runtime, now_ms=1_000)
    store.update("sw", state="paused")
    runtime.statuses["engineer@a1b2c3-0001"] = "idle"
    for n in range(IDLE_KILL_TICKS + 2):
        idle.beat(store.redis, "sw", "engineer@a1b2c3-0001", idle.WORKING, 2_000 + n * 60_000)
        tick("sw", store, ledger, runtime, now_ms=2_000 + n * 60_000)
    assert runtime.nudged == [] and runtime.killed == []


def test_a_stalled_agents_open_messages_move_to_its_seat_for_the_next_engineer(store):
    from scripts.inbox.store import InboxStore
    from scripts.swarm.stale_nudge import is_nudge
    from scripts.swarm.tick import IDLE_KILL_TICKS

    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    seated(store, ledger, runtime, "t1")
    tick("sw", store, ledger, runtime, now_ms=1_000)
    store.update("sw", state="paused")
    inbox = InboxStore(store.redis)
    open_item = inbox.send("master@a1b2c3-0001", "engineer@a1b2c3-0001", "your pull request has a red check")
    closed = inbox.send("master@a1b2c3-0001", "engineer@a1b2c3-0001", "old news")
    inbox.close(closed.id, "engineer@a1b2c3-0001", "done", "handled the request")
    runtime.statuses["engineer@a1b2c3-0001"] = "idle"
    idle_for(store, ledger, runtime, IDLE_KILL_TICKS, start=2_000)
    assert runtime.killed == ["engineer@a1b2c3-0001"]
    seat = [(i.id, i.state) for i in inbox.inbox("eng-1@sw") if not is_nudge(i)]
    assert seat == [(open_item.id, "pending")] and len(stale_nudges(store, "eng-1@sw")) == 1
    assert [i.id for i in inbox.inbox("engineer@a1b2c3-0001")] == [closed.id]
