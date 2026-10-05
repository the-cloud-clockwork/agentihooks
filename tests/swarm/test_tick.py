from dataclasses import replace

import pytest

from scripts.swarm.store import MASTER, AgentRecord, RedisStore, SwarmConfig
from scripts.swarm.tick import STARTUP_GRACE_MS, Placed, SpawnError, tick

pytestmark = pytest.mark.xdist_group("fakeredis")


class FakeLedger:
    def __init__(self, tasks):
        self.rows = {
            t["id"]: {"state": "open", "claimed_by": "", "out_of_scope": False, "lane": "eng", **t} for t in tasks
        }
        self.notes = []
        self.swarm_sized = []

    def tasks(self, slug):
        return list(self.rows.values())

    def events(self, slug):
        return []

    def state(self, slug):
        log = getattr(self, "log", [])
        return {"tasks": self.tasks(slug), "_meta": {"rev": len(log), "events": log}, "followups": []}

    def update_task(self, slug, task_id, fields, by="swarm"):
        self.rows[task_id].update(fields)

    def notify(self, slug, text):
        self.notes.append(text)

    def mark_swarm(self, slug):
        self.swarm_sized.append(slug)


class FakeRuntime:
    def __init__(self, fail=False, full=False, crash=None):
        self.live, self.spawned, self.killed, self.closed, self.nudged = set(), [], [], [], []
        self.tasks, self.masters, self.spawns_seen, self.harness = [], [], [], "claude"
        self.fail, self.full, self.crash, self.statuses, self.stuck = fail, full, crash, {}, set()
        self.conversation_ids, self.named, self.closed_spaces = {}, [], []

    def has_capacity(self):
        return not self.full

    def spawn(self, config, lane, name, task, spawns=None):
        if self.crash:
            raise self.crash
        if self.fail:
            raise SpawnError("herdr down")
        self.live.add(name)
        if lane == MASTER:
            self.masters.append((name, dict(task)))
            return Placed(pane_id=f"w1:m{len(self.masters)}", harness="claude")
        self.spawned.append((lane, name, task["id"]))
        self.tasks.append(dict(task))
        self.spawns_seen.append(dict(spawns or {}))
        return Placed(
            pane_id=f"w1:p{len(self.spawned)}", harness=self.harness, account="acct", model="opus", effort="high"
        )

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

    def name_pane(self, agent):
        self.named.append(agent.name)
        return False

    def conversations(self):
        return None if self.conversation_ids is None else dict(self.conversation_ids)

    def close_space(self, config):
        self.closed_spaces.append(config.slug)
        return True


@pytest.fixture
def store():
    import fakeredis

    s = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    s.create(SwarmConfig("sw", "/repo", max_eng=2, max_ci=1))
    return s


def workers(store):
    return [a for a in store.agents("sw") if a.lane != MASTER]


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
    first = workers(store)[0]
    store.put_agent("sw", AgentRecord(**{**first.__dict__, "state": "finished"}))
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert runtime.killed == ["sw-eng-1"]
    assert runtime.spawned[-1] == ("eng", "sw-eng-3", "t3")


def test_scale_down_waits_for_the_task_to_finish(store):
    ledger, runtime = tasks(("t1", "eng"), ("t2", "eng"), ("t3", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    store.update("sw", max_eng=1)
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert runtime.killed == [] and len(workers(store)) == 2


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
    assert ledger.rows["t1"]["state"] == "open" and store.claimant("sw", "t1") is None and workers(store) == []


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


def test_a_reopened_task_keeps_its_pull_request_link(store):
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    ledger.rows["t1"].update(state="pr", pr_url="https://github.com/o/r/pull/3")
    runtime.live.clear()
    runtime.full = True
    tick("sw", store, ledger, runtime, now_ms=1_000 + STARTUP_GRACE_MS + 1)
    assert ledger.rows["t1"]["state"] == "open" and ledger.rows["t1"]["pr_url"].endswith("/3")


@pytest.mark.parametrize(("pr_url", "state"), [("https://github.com/o/r/pull/3", "pr"), ("", "claimed")])
def test_a_reclaimed_task_is_in_pr_state_only_when_it_has_a_pull_request(store, pr_url, state):
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    ledger.rows["t1"].update(state="pr" if pr_url else "claimed", pr_url=pr_url)
    first = workers(store)[0]
    store.put_handoff("sw", "t1", "pushed, waiting on checks")
    store.put_agent("sw", AgentRecord(**{**first.__dict__, "state": "finished"}))
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert runtime.spawned[-1] == ("eng", "sw-eng-2", "t1")
    assert (ledger.rows["t1"]["state"], ledger.rows["t1"]["claimed_by"]) == (state, "sw-eng-2")


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


@pytest.mark.parametrize("state", ["pending", "delivered", "read"])
def test_a_retired_stalled_agent_leaves_its_items_on_its_seat(store, state):
    from scripts.inbox.store import InboxStore
    from scripts.swarm.tick import IDLE_KILL_TICKS

    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    inbox = InboxStore(store.redis)
    item = inbox.send("sw-ci-9", "sw-eng-1", "contract confirmed")
    if state != "pending":
        getattr(inbox, "deliver" if state == "delivered" else "read")(item.id, "sw-eng-1")
    runtime.statuses["sw-eng-1"] = "idle"
    store.update("sw", state="paused")
    for n in range(IDLE_KILL_TICKS):
        tick("sw", store, ledger, runtime, now_ms=2_000 + n)
    assert runtime.killed == ["sw-eng-1"]
    assert (inbox.get(item.id).address, inbox.get(item.id).state) == ("eng-1@sw", "pending")


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
    first = workers(store)[0]
    store.put_agent("sw", AgentRecord(**{**first.__dict__, "state": "finished"}))
    runtime.stuck.add("sw-eng-1")
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert [a.name for a in workers(store)] == ["sw-eng-1"]


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
    first = workers(store)[0]
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
    (agent,) = workers(store)
    assert (agent.model, agent.effort) == ("opus", "high")


def masters(store):
    return [a for a in store.agents("sw") if a.lane == MASTER]


def test_a_running_swarm_spawns_one_master_that_claims_no_task(store):
    ledger, runtime = tasks(("t1", "eng"), ("t2", "eng"), ("t3", "eng")), FakeRuntime()
    assert "spawned master sw-master-1" in tick("sw", store, ledger, runtime, 1)
    tick("sw", store, ledger, runtime, 2)
    assert [name for name, _ in runtime.masters] == ["sw-master-1"]
    assert [(a.name, a.task) for a in masters(store)] == [("sw-master-1", MASTER)]
    assert len(runtime.spawned) == 2
    assert {r["claimed_by"] for r in ledger.rows.values()} == {"sw-eng-1", "sw-eng-2", ""}
    assert all(store.claimant("sw", t) != "sw-master-1" for t in ledger.rows)


def test_a_paused_or_drained_swarm_keeps_its_master_and_a_stopped_one_has_none(store):
    store.update("sw", state="paused")
    runtime = FakeRuntime()
    tick("sw", store, tasks(), runtime, 1)
    assert [a.name for a in masters(store)] == ["sw-master-1"]
    store.update("sw", state="running")
    assert "drained" in tick("sw", store, tasks(), runtime, 2)
    assert store.config("sw").state == "drained" and [a.name for a in masters(store)] == ["sw-master-1"]
    store.update("sw", state="stopped")
    store.drop_agent("sw", "sw-master-1")
    assert tick("sw", store, tasks(), runtime, 3) == [] and len(runtime.masters) == 1


def test_an_operator_write_to_a_stopped_swarm_starts_its_master_and_no_engineers(store):
    from scripts.inbox.store import InboxStore

    store.update("sw", state="stopped")
    InboxStore(store.redis).send("operator", "master@sw", "operator replied on the ledger")
    runtime = FakeRuntime()
    actions = tick("sw", store, tasks(("t1", "eng"), ("t2", "ci")), runtime, 1)
    assert "spawned master sw-master-1" in actions
    assert [a.name for a in masters(store)] == ["sw-master-1"] and workers(store) == []
    assert runtime.spawned == [] and store.config("sw").state == "paused"


def test_a_drained_swarm_without_a_master_starts_one_and_no_engineers(store):
    store.update("sw", state="drained")
    runtime = FakeRuntime()
    actions = tick("sw", store, tasks(), runtime, 1)
    assert "spawned master sw-master-1" in actions and runtime.spawned == []
    assert store.config("sw").state == "drained"


def test_a_stopped_swarm_with_nothing_for_its_master_stays_down(store):
    store.update("sw", state="stopped")
    runtime = FakeRuntime()
    assert tick("sw", store, tasks(("t1", "eng")), runtime, 1) == []
    assert runtime.masters == [] and store.config("sw").state == "stopped"


def test_a_dead_master_is_respawned(store):
    runtime = FakeRuntime()
    tick("sw", store, tasks(), runtime, 1)
    runtime.live.clear()
    actions = tick("sw", store, tasks(), runtime, 1 + STARTUP_GRACE_MS + 1)
    assert "lost sw-master-1" in actions and "spawned master sw-master-2" in actions
    assert [a.name for a in masters(store)] == ["sw-master-2"]


def test_an_idle_master_is_never_nudged_or_stalled(store):
    runtime = FakeRuntime()
    tick("sw", store, tasks(), runtime, 1)
    runtime.statuses["sw-master-1"] = "idle"
    for n in range(12):
        tick("sw", store, tasks(), runtime, 2 + n)
    assert runtime.nudged == [] and [a.name for a in masters(store)] == ["sw-master-1"]


def test_the_tick_names_a_live_masters_pane(store):
    runtime = FakeRuntime()
    tick("sw", store, tasks(), runtime, 1)
    tick("sw", store, tasks(), runtime, 2)
    assert runtime.named == ["sw-master-1"]


def test_a_master_handoff_retires_the_old_master_and_spawns_the_next_with_the_doc(store):
    runtime = FakeRuntime()
    tick("sw", store, tasks(), runtime, 1)
    (old,) = masters(store)
    store.put_handoff("sw", MASTER, "operator wants caps at four")
    store.put_agent("sw", replace(old, state="finished"))
    actions = tick("sw", store, tasks(), runtime, 2)
    assert "retired sw-master-1" in actions and "spawned master sw-master-2" in actions
    assert runtime.killed == ["sw-master-1"]
    assert runtime.masters[-1][1]["handoff"] == "operator wants caps at four"
    assert store.handoff("sw", MASTER) == ""
    assert [a.name for a in masters(store)] == ["sw-master-2"]


def test_a_full_house_waits_for_a_slot_before_starting_the_master(store):
    assert "no session slot for the master, waiting" in tick("sw", store, tasks(), FakeRuntime(full=True), 1)
    assert masters(store) == []


def test_a_stopping_swarm_keeps_its_master_until_the_last_worker_leaves(store):
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, 1)
    store.update("sw", state="stopping")
    tick("sw", store, ledger, runtime, 2)
    assert [a.name for a in masters(store)] == ["sw-master-1"]
    store.put_agent("sw", replace(workers(store)[0], state="finished"))
    actions = tick("sw", store, ledger, runtime, 3)
    assert "retired sw-master-1" in actions and actions[-1] == "stopped"
    assert store.agents("sw") == [] and store.config("sw").state == "stopped"


def spawned_ids(runtime):
    return [task_id for _, _, task_id in runtime.spawned]


def test_a_task_waits_on_an_open_dependency_and_is_claimed_once_it_is_done(store):
    ledger = FakeLedger([{"id": "t1"}, {"id": "t2", "depends_on": ["t1"]}])
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert spawned_ids(runtime) == ["t1"] and ledger.rows["t2"]["state"] == "open"
    ledger.rows["t1"]["state"] = "done"
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert spawned_ids(runtime) == ["t1", "t2"]


def test_overlapping_territories_are_never_claimed_together(store):
    ledger = FakeLedger(
        [
            {"id": "t1", "territory": ["scripts/swarm"]},
            {"id": "t2", "territory": ["scripts/swarm/tick.py"]},
            {"id": "t3", "lane": "ci", "territory": ["scripts/swarm/"]},
        ]
    )
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert spawned_ids(runtime) == ["t1"]
    ledger.rows["t1"]["state"] = "pr"
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert spawned_ids(runtime) == ["t1"]
    ledger.rows["t1"]["state"] = "done"
    tick("sw", store, ledger, runtime, now_ms=3_000)
    assert spawned_ids(runtime) == ["t1", "t2"]


def test_territories_that_only_share_a_name_prefix_do_not_overlap(store):
    ledger = FakeLedger(
        [{"id": "t1", "territory": ["scripts/swarm"]}, {"id": "t2", "territory": ["scripts/swarm_ledger"]}]
    )
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert spawned_ids(runtime) == ["t1", "t2"]


def test_a_task_without_territory_is_claimed_alongside_anything(store):
    ledger = FakeLedger(
        [{"id": "t1", "territory": ["hooks"]}, {"id": "t2"}, {"id": "t3", "lane": "ci", "territory": []}]
    )
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert spawned_ids(runtime) == ["t1", "t2", "t3"]


def test_a_swarm_whose_only_open_task_waits_on_a_blocked_one_drains(store):
    ledger = FakeLedger([{"id": "t1", "state": "blocked"}, {"id": "t2", "depends_on": ["t1"]}])
    runtime = FakeRuntime()
    assert "drained" in tick("sw", store, ledger, runtime, now_ms=1_000)
    assert runtime.spawned == []


def seats_of(store):
    return {a.name: a.seat for a in store.agents("sw")}


def test_the_master_and_lane_agents_are_seated_in_free_slots(store):
    ledger, runtime = tasks(("t1", "eng"), ("t2", "eng"), ("t4", "ci")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert seats_of(store) == {
        "sw-master-1": "master@sw",
        "sw-eng-1": "eng-1@sw",
        "sw-eng-2": "eng-2@sw",
        "sw-ci-1": "ci-1@sw",
    }
    assert store.seats.occupant("eng-2@sw").occupant == "sw-eng-2"


def test_a_freed_slot_is_taken_by_the_next_agent_with_a_new_generation(store):
    ledger, runtime = tasks(("t1", "eng"), ("t2", "eng"), ("t3", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    ledger.rows["t1"]["state"] = "done"
    first = workers(store)[0]
    store.put_agent("sw", replace(first, state="finished"))
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert seats_of(store)["sw-eng-3"] == "eng-1@sw"
    assert [(e["generation"], e["occupant"]) for e in store.seats.history("eng-1@sw")] == [
        (1, "sw-eng-1"),
        (2, "sw-eng-3"),
    ]


def test_a_handoff_successor_takes_its_predecessors_seat(store):
    ledger, runtime = tasks(("t1", "eng"), ("t2", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    one, two = workers(store)
    ledger.rows["t1"]["state"] = "done"
    store.put_agent("sw", replace(one, state="finished"))
    store.put_handoff("sw", "t2", "halfway", seat=two.seat)
    store.put_agent("sw", replace(two, state="finished"))
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert runtime.spawned[-1] == ("eng", "sw-eng-3", "t2")
    assert seats_of(store)["sw-eng-3"] == "eng-2@sw"
    assert store.handoff_seat("sw", "t2") == ""


def test_a_master_handoff_keeps_the_master_seat_and_bumps_its_generation(store):
    runtime = FakeRuntime()
    tick("sw", store, tasks(), runtime, 1)
    (old,) = masters(store)
    store.put_handoff("sw", MASTER, "doc", seat=old.seat)
    store.put_agent("sw", replace(old, state="finished"))
    tick("sw", store, tasks(), runtime, 2)
    assert store.seats.occupant("master@sw").occupant == "sw-master-2"
    assert [e["generation"] for e in store.seats.history("master@sw")] == [1, 2]


def test_a_successor_waits_for_a_stuck_predecessor_then_takes_its_seat(store):
    ledger, runtime = tasks(("t1", "eng"), ("t2", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    one, two = workers(store)
    ledger.rows["t1"]["state"] = "done"
    store.put_agent("sw", replace(one, state="finished"))
    store.put_handoff("sw", "t2", "halfway", seat=two.seat)
    store.put_agent("sw", replace(two, state="finished"))
    runtime.stuck.add(two.name)
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert [s[2] for s in runtime.spawned] == ["t1", "t2"]
    runtime.stuck.clear()
    tick("sw", store, ledger, runtime, now_ms=3_000)
    assert runtime.spawned[-1] == ("eng", "sw-eng-3", "t2") and seats_of(store)["sw-eng-3"] == "eng-2@sw"


def test_a_failed_spawn_leaves_the_seat_with_a_new_generation_and_the_task_open(store):
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime(fail=True)
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert ledger.rows["t1"]["state"] == "open"
    assert [e["occupant"] for e in store.seats.history("eng-1@sw")] == ["sw-eng-1"]


def test_a_task_without_a_kind_takes_its_lane_default_kind_when_claimed(store):
    store.update("sw", lanes={"eng": {"kind": "research"}, "ci": {"kind": "auto"}})
    ledger = FakeLedger([{"id": "t1"}, {"id": "t2", "kind": "ops"}, {"id": "t3", "lane": "ci"}])
    tick("sw", store, ledger, FakeRuntime(), now_ms=1_000)
    assert [ledger.rows[t].get("kind") for t in ("t1", "t2", "t3")] == ["research", "ops", None]


def test_work_lane_spawns_are_counted_by_harness_and_handed_to_the_runtime(store):
    rt = FakeRuntime()
    tick("sw", store, FakeLedger([{"id": "t1"}, {"id": "t2"}, {"id": "t3", "lane": "ci"}]), rt, 1000)
    assert rt.spawns_seen == [{}, {"claude": 1}, {"claude": 2}]
    rt.harness = "codex"
    store.update("sw", max_eng=3)
    tick("sw", store, FakeLedger([{"id": "t4"}]), rt, 2000)
    assert store.spawns("sw") == {"claude": 3, "codex": 1}


def test_a_master_spawn_is_not_counted_in_the_codex_share(store):
    store.update("sw", max_eng=0, max_ci=0)
    tick("sw", store, FakeLedger([]), FakeRuntime(), 1000)
    assert store.spawns("sw") == {}


def _conversations(store):
    return {a.name: a.conversation_id for a in store.agents("sw")}


def test_a_spawned_agent_gets_the_conversation_id_herdr_reports_for_its_pane(store):
    rt = FakeRuntime()
    rt.conversation_ids = {"w1:p1": "5c90d80c", "w1:m1": "15e33356"}
    tick("sw", store, FakeLedger([{"id": "t1"}]), rt, 1000)
    assert _conversations(store) == {"sw-eng-1": "5c90d80c", "sw-master-1": "15e33356"}


def test_each_tick_refreshes_the_conversation_id_and_stores_unknown_as_empty(store):
    rt = FakeRuntime()
    ledger = FakeLedger([{"id": "t1"}, {"id": "t2"}])
    tick("sw", store, ledger, rt, 1000)
    assert _conversations(store) == {"sw-eng-1": "", "sw-eng-2": "", "sw-master-1": ""}
    rt.conversation_ids = {"w1:p1": "first", "w1:p2": "other"}
    tick("sw", store, ledger, rt, 2000)
    assert _conversations(store) == {"sw-eng-1": "first", "sw-eng-2": "other", "sw-master-1": ""}
    rt.conversation_ids = {"w1:p1": "resumed", "w1:p2": ""}
    tick("sw", store, ledger, rt, 3000)
    assert _conversations(store) == {"sw-eng-1": "resumed", "sw-eng-2": "", "sw-master-1": ""}
    del rt.conversation_ids["w1:p1"]
    tick("sw", store, ledger, rt, 4000)
    assert _conversations(store)["sw-eng-1"] == ""


def test_a_tick_without_an_answer_from_herdr_keeps_the_known_conversation_ids(store):
    rt = FakeRuntime()
    rt.conversation_ids = {"w1:p1": "5c90d80c"}
    ledger = FakeLedger([{"id": "t1"}])
    tick("sw", store, ledger, rt, 1000)
    rt.conversation_ids = None
    tick("sw", store, ledger, rt, 2000)
    assert _conversations(store)["sw-eng-1"] == "5c90d80c"


def idle_for(store, ledger, runtime, ticks, start):
    for n in range(ticks):
        tick("sw", store, ledger, runtime, now_ms=start + n * 60_000)


def test_an_idle_agent_under_a_declared_wait_is_never_nudged_or_retired(store):
    from scripts.swarm import idle
    from scripts.swarm.tick import IDLE_KILL_TICKS

    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    store.update("sw", state="paused")
    runtime.statuses["sw-eng-1"] = "idle"
    idle.declare_wait(store.redis, "sw", "sw-eng-1", 1_000 + 15 * 60_000 + IDLE_KILL_TICKS * 60_000, "deploy", 1_000)
    idle_for(store, ledger, runtime, 15 + IDLE_KILL_TICKS, start=2_000)
    assert runtime.nudged == [] and runtime.killed == [] and ledger.rows["t1"]["state"] == "claimed"


def test_a_wait_that_ended_lets_the_idle_count_run_again(store):
    from scripts.swarm import idle
    from scripts.swarm.tick import IDLE_KILL_TICKS

    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    store.update("sw", state="paused")
    runtime.statuses["sw-eng-1"] = "idle"
    idle.declare_wait(store.redis, "sw", "sw-eng-1", 2_000, "deploy", 1_000)
    store.redis.persist(idle.key("sw", "wait", "sw-eng-1"))
    idle_for(store, ledger, runtime, IDLE_KILL_TICKS, start=3_000)
    assert runtime.killed == ["sw-eng-1"]


def test_an_agent_whose_heartbeat_says_working_is_never_retired_while_its_pane_reads_idle(store):
    from scripts.swarm import idle
    from scripts.swarm.tick import IDLE_KILL_TICKS

    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    store.update("sw", state="paused")
    runtime.statuses["sw-eng-1"] = "idle"
    for n in range(IDLE_KILL_TICKS + 2):
        idle.beat(store.redis, "sw", "sw-eng-1", idle.WORKING, 2_000 + n * 60_000)
        tick("sw", store, ledger, runtime, now_ms=2_000 + n * 60_000)
    assert runtime.nudged == [] and runtime.killed == []


def test_a_stalled_agents_open_messages_move_to_its_seat_for_the_next_engineer(store):
    from scripts.inbox.store import InboxStore
    from scripts.swarm.tick import IDLE_KILL_TICKS

    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    store.update("sw", state="paused")
    inbox = InboxStore(store.redis)
    open_item = inbox.send("sw-master-1", "sw-eng-1", "your pull request has a red check")
    closed = inbox.send("sw-master-1", "sw-eng-1", "old news")
    inbox.close(closed.id, "sw-eng-1", "done")
    runtime.statuses["sw-eng-1"] = "idle"
    idle_for(store, ledger, runtime, IDLE_KILL_TICKS, start=2_000)
    assert runtime.killed == ["sw-eng-1"]
    assert [(i.id, i.state) for i in inbox.inbox("eng-1@sw")] == [(open_item.id, "pending")]
    assert [i.id for i in inbox.inbox("sw-eng-1")] == [closed.id]
