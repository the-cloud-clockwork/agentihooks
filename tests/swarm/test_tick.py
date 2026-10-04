import fakeredis
import pytest

from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from scripts.swarm.tick import STARTUP_GRACE_MS, Placed, SpawnError, tick


class FakeLedger:
    def __init__(self, tasks):
        self.rows = {
            t["id"]: {"state": "open", "claimed_by": "", "out_of_scope": False, "lane": "eng", **t} for t in tasks
        }
        self.notes = []

    def tasks(self, slug):
        return list(self.rows.values())

    def update_task(self, slug, task_id, fields, by="swarm"):
        self.rows[task_id].update(fields)

    def notify(self, slug, text):
        self.notes.append(text)


class FakeRuntime:
    def __init__(self, fail=False, full=False, crash=None):
        self.live, self.spawned, self.killed, self.closed, self.nudged = set(), [], [], [], []
        self.tasks = []
        self.fail, self.full, self.crash, self.statuses, self.stuck = fail, full, crash, {}, set()

    def has_capacity(self):
        return not self.full

    def spawn(self, config, lane, name, task):
        if self.crash:
            raise self.crash
        if self.fail:
            raise SpawnError("herdr down")
        self.spawned.append((lane, name, task["id"]))
        self.tasks.append(dict(task))
        self.live.add(name)
        return Placed(pane_id=f"w1:p{len(self.spawned)}", harness="claude", account="acct", model="opus", effort="high")

    def live_names(self):
        return set(self.live)

    def retire(self, agent, live):
        if agent.name in self.stuck:
            return False
        if live:
            self.killed.append(agent.name)
            self.live.discard(agent.name)
        self.closed.append(agent.pane_id)
        return True

    def status(self, agent):
        return self.statuses.get(agent.name, "working")

    def nudge(self, agent, text):
        self.nudged.append(agent.name)


@pytest.fixture
def store():
    s = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    s.create(SwarmConfig("sw", "/repo", max_eng=2, max_ci=1))
    return s


def tasks(*specs):
    return FakeLedger([{"id": i, "lane": lane} for i, lane in specs])


def test_scale_up_is_immediate_and_capped_per_lane(store):
    ledger, runtime = tasks(("t1", "eng"), ("t2", "eng"), ("t3", "eng"), ("t4", "ci"), ("t5", "ci")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert runtime.spawned == [("eng", "sw-eng-1", "t1"), ("eng", "sw-eng-2", "t2"), ("ci", "sw-ci-1", "t4")]
    assert [ledger.rows[t]["claimed_by"] for t in ("t1", "t2", "t3", "t4")] == ["sw-eng-1", "sw-eng-2", "", "sw-ci-1"]
    assert store.claimant("sw", "t1") == "sw-eng-1"


def test_a_second_tick_does_not_overspawn(store):
    ledger, runtime = tasks(("t1", "eng"), ("t2", "eng"), ("t3", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert len(runtime.spawned) == 2


def test_a_finished_agent_is_retired_and_replaced_while_work_remains(store):
    ledger, runtime = tasks(("t1", "eng"), ("t2", "eng"), ("t3", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    ledger.rows["t1"]["state"] = "done"
    first = store.agents("sw")[0]
    store.put_agent("sw", AgentRecord(**{**first.__dict__, "state": "finished"}))
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert runtime.killed == ["sw-eng-1"]
    assert runtime.spawned[-1] == ("eng", "sw-eng-3", "t3")


def test_scale_down_waits_for_the_task_to_finish(store):
    ledger, runtime = tasks(("t1", "eng"), ("t2", "eng"), ("t3", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    store.update("sw", max_eng=1)
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert runtime.killed == [] and len(store.agents("sw")) == 2


def test_a_dead_agent_frees_its_task_after_the_startup_grace(store):
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    runtime.live.clear()
    tick("sw", store, ledger, runtime, now_ms=1_000 + STARTUP_GRACE_MS - 1)
    assert ledger.rows["t1"]["claimed_by"] == "sw-eng-1" and len(runtime.spawned) == 1
    tick("sw", store, ledger, runtime, now_ms=1_000 + STARTUP_GRACE_MS + 1)
    assert ledger.rows["t1"]["state"] == "claimed" and ledger.rows["t1"]["claimed_by"] == "sw-eng-2"
    assert runtime.spawned[-1] == ("eng", "sw-eng-2", "t1")


def test_paused_spawns_nothing_and_stopping_ends_stopped_when_empty(store):
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    store.update("sw", state="paused")
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert runtime.spawned == []
    store.update("sw", state="stopping")
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert store.config("sw").state == "stopped"


def test_out_of_scope_blocked_and_claimed_tasks_are_not_spawned_for(store):
    ledger, runtime = tasks(("t1", "eng"), ("t2", "eng"), ("t3", "eng")), FakeRuntime()
    ledger.rows["t1"]["out_of_scope"] = True
    ledger.rows["t2"]["state"] = "blocked"
    store.claim("sw", "t3", "someone", 60_000)
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert runtime.spawned == []


def test_drains_when_nothing_is_left_and_tells_the_operator_about_blocked_tasks(store):
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    ledger.rows["t1"]["state"] = "blocked"
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert store.config("sw").state == "drained"
    assert ledger.notes and "blocked" in ledger.notes[0]


@pytest.mark.parametrize("runtime", [FakeRuntime(fail=True), FakeRuntime(crash=OSError("disk full"))])
def test_a_failed_spawn_of_any_kind_reopens_the_task(store, runtime):
    ledger = tasks(("t1", "eng"))
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert ledger.rows["t1"]["state"] == "open" and store.claimant("sw", "t1") is None and store.agents("sw") == []


def test_no_free_session_slot_claims_nothing(store):
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime(full=True)
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert ledger.rows["t1"]["state"] == "open" and store.claimant("sw", "t1") is None and runtime.spawned == []


def test_a_dead_agent_with_an_open_pull_request_hands_the_task_back(store):
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    ledger.rows["t1"].update(state="pr", pr_url="https://github.com/o/r/pull/3")
    runtime.live.clear()
    tick("sw", store, ledger, runtime, now_ms=1_000 + STARTUP_GRACE_MS + 1)
    assert runtime.spawned[-1] == ("eng", "sw-eng-2", "t1") and ledger.rows["t1"]["pr_url"].endswith("/3")
    assert "w1:p1" in runtime.closed


def test_a_claimed_task_without_an_agent_is_reopened(store):
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    store.update("sw", state="paused")
    ledger.rows["t1"].update(state="claimed", claimed_by="sw-eng-9")
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert ledger.rows["t1"]["state"] == "open"


def test_an_idle_agent_is_nudged_then_retired_and_its_task_reopened(store):
    from scripts.swarm.tick import IDLE_KILL_TICKS, IDLE_NUDGE_TICKS

    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    runtime.statuses["sw-eng-1"] = "idle"
    store.update("sw", state="paused")
    for n in range(IDLE_KILL_TICKS):
        tick("sw", store, ledger, runtime, now_ms=2_000 + n)
        if n + 1 == IDLE_NUDGE_TICKS:
            assert runtime.nudged == ["sw-eng-1"]
    assert runtime.killed == ["sw-eng-1"] and ledger.rows["t1"]["state"] == "open"


def test_a_busy_turn_resets_the_idle_count(store):
    from scripts.swarm.tick import IDLE_NUDGE_TICKS

    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    for status in ["idle"] * (IDLE_NUDGE_TICKS - 1) + ["working"] + ["idle"] * (IDLE_NUDGE_TICKS - 1):
        runtime.statuses["sw-eng-1"] = status
        tick("sw", store, ledger, runtime, now_ms=2_000)
    assert runtime.nudged == []


def test_a_finished_agent_that_will_not_die_stays_registered(store):
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    first = store.agents("sw")[0]
    store.put_agent("sw", AgentRecord(**{**first.__dict__, "state": "finished"}))
    runtime.stuck.add("sw-eng-1")
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert [a.name for a in store.agents("sw")] == ["sw-eng-1"]


def test_a_drained_swarm_wakes_up_for_new_tasks(store):
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    ledger.rows["t1"]["state"] = "done"
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert store.config("sw").state == "drained"
    ledger.rows["t2"] = {"id": "t2", "lane": "eng", "state": "open", "claimed_by": "", "out_of_scope": False}
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert store.config("sw").state == "running" and runtime.spawned == [("eng", "sw-eng-1", "t2")]


def test_a_handed_off_task_is_reopened_and_respawned_with_the_doc(store):
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    first = store.agents("sw")[0]
    store.put_handoff("sw", "t1", "seam 1 green, seam 2 red")
    store.put_agent("sw", AgentRecord(**{**first.__dict__, "state": "finished"}))
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert runtime.killed == ["sw-eng-1"]
    assert runtime.spawned[-1] == ("eng", "sw-eng-2", "t1")
    assert runtime.tasks[-1]["handoff"] == "seam 1 green, seam 2 red"
    assert ledger.rows["t1"]["claimed_by"] == "sw-eng-2"
    assert store.handoff("sw", "t1") == ""


def test_a_spawned_agent_keeps_the_model_and_effort_it_was_placed_with(store):
    tick("sw", store, tasks(("t1", "eng")), FakeRuntime(), now_ms=1_000)
    (agent,) = store.agents("sw")
    assert (agent.model, agent.effort) == ("opus", "high")
