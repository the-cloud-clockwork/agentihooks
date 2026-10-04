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
    def __init__(self, fail=False):
        self.live, self.spawned, self.killed, self.fail = set(), [], [], fail

    def spawn(self, config, lane, name, task):
        if self.fail:
            raise SpawnError("herdr down")
        self.spawned.append((lane, name, task["id"]))
        self.live.add(name)
        return Placed(pane_id=f"w1:p{len(self.spawned)}", harness="claude", account="acct")

    def live_names(self):
        return set(self.live)

    def terminate(self, name):
        self.killed.append(name)
        self.live.discard(name)


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


def test_a_failed_spawn_reopens_the_task(store):
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime(fail=True)
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert ledger.rows["t1"]["state"] == "open" and store.claimant("sw", "t1") is None and store.agents("sw") == []
