from dataclasses import replace

import pytest

from scripts.swarm.store import MASTER, AgentRecord, RedisStore, SwarmConfig
from scripts.swarm.tick import MASTER_DOWN, STARTUP_GRACE_MS, Placed, SpawnError, tick

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
        return {
            "overview": "Project intent",
            "tasks": self.tasks(slug),
            "phases": getattr(self, "phases", []),
            "_meta": {"rev": len(log), "events": log},
            "followups": [],
        }

    def update_task(self, slug, task_id, fields, by="swarm", if_state=()):
        assert slug == "sw"
        row = self.rows[task_id]
        if not if_state or row["state"] in if_state:
            row.update(fields)
        return dict(row)

    def notify(self, slug, text):
        assert slug == "sw"
        self.notes.append(text)

    def mark_swarm(self, slug):
        self.swarm_sized.append(slug)

    def binned(self, slug):
        return slug in getattr(self, "bin", set())


class FakeRuntime:
    def __init__(self, fail=False, full=False, crash=None):
        self.live, self.spawned, self.killed, self.closed, self.nudged = set(), [], [], [], []
        self.tasks, self.masters, self.spawns_seen, self.harness = [], [], [], "claude"
        self.fail, self.full, self.crash, self.statuses, self.stuck = fail, full, crash, {}, set()
        self.conversation_ids, self.named, self.closed_spaces, self.typed = {}, [], [], {}
        self.capacity_for = []

    def has_capacity(self, config):
        self.capacity_for.append(config.slug)
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
            pane_id=f"w1:p{len(self.spawned)}",
            harness=self.harness,
            account="acct",
            model="opus",
            effort="high",
            choice="share",
        )

    def recover(self, name):
        return Placed("", "claude")

    def live_names(self):
        return set(self.live)

    def reported(self, agent):
        return agent.name in self.live

    def bindings(self, agents):
        from scripts.swarm.live_binding import assignment

        return {a.name: {**assignment(a), "hooks": True} for a in agents if a.name in self.live}

    def pane_open(self, agent):
        return bool(agent.pane_id) and agent.pane_id not in self.closed

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

    def observe(self, agent):
        from scripts.swarm.pane import PaneObservation

        return PaneObservation(self.status(agent), typed=self.typed.get(agent.name, ""))

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
    assert runtime.spawned == [
        ("eng", "engineer@a1b2c3-0001", "t1"),
        ("eng", "engineer@a1b2c3-0002", "t2"),
        ("ci", "ci@a1b2c3-0001", "t4"),
    ]
    assert [ledger.rows[t]["claimed_by"] for t in ("t1", "t2", "t3", "t4")] == [
        "engineer@a1b2c3-0001",
        "engineer@a1b2c3-0002",
        "",
        "ci@a1b2c3-0001",
    ]
    assert store.claimant("sw", "t1") == "engineer@a1b2c3-0001"


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
    assert runtime.killed == ["engineer@a1b2c3-0001"]
    assert runtime.spawned[-1] == ("eng", "engineer@a1b2c3-0003", "t3")


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
    assert ledger.rows["t1"]["claimed_by"] == "engineer@a1b2c3-0001" and len(runtime.spawned) == 1
    tick("sw", store, ledger, runtime, now_ms=1_000 + STARTUP_GRACE_MS + 1)
    assert ledger.rows["t1"]["state"] == "claimed" and ledger.rows["t1"]["claimed_by"] == "engineer@a1b2c3-0002"
    assert runtime.spawned[-1] == ("eng", "engineer@a1b2c3-0002", "t1")


def test_paused_spawns_nothing_and_stopping_ends_stopped_when_empty(store, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_MASTER_RETIRE_HANDOFF_MINUTES", "0")
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
    assert runtime.capacity_for == ["sw", "sw"]


def test_a_dead_agent_with_an_open_pull_request_hands_the_task_back(store):
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    ledger.rows["t1"].update(state="pr", pr_url="https://github.com/o/r/pull/3")
    runtime.live.clear()
    tick("sw", store, ledger, runtime, now_ms=1_000 + STARTUP_GRACE_MS + 1)
    assert runtime.spawned[-1] == ("eng", "engineer@a1b2c3-0002", "t1") and ledger.rows["t1"]["pr_url"].endswith("/3")
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
    assert runtime.spawned[-1] == ("eng", "engineer@a1b2c3-0002", "t1")
    assert (ledger.rows["t1"]["state"], ledger.rows["t1"]["claimed_by"]) == (state, "engineer@a1b2c3-0002")


class DoneMidTick(FakeLedger):
    def __init__(self, tasks):
        super().__init__(tasks)
        self.closing = None

    def state(self, slug):
        snapshot = {**super().state(slug), "tasks": [dict(row) for row in self.rows.values()]}
        if self.closing:
            self.closing()
            self.closing = None
        return snapshot


def test_a_task_closed_done_during_a_tick_stays_done_and_is_not_claimed_again(store):
    ledger, runtime = (
        DoneMidTick([{"id": "t1", "lane": "eng", "pr_url": "https://github.com/o/r/pull/3"}]),
        FakeRuntime(),
    )
    tick("sw", store, ledger, runtime, now_ms=1_000)
    agent = workers(store)[0]

    def swarm_done():
        ledger.rows["t1"].update(state="done", done=True)
        store.release("sw", "t1", agent.name)
        store.put_agent("sw", replace(agent, state="finished"))

    ledger.closing = swarm_done
    actions = tick("sw", store, ledger, runtime, now_ms=2_000)
    tick("sw", store, ledger, runtime, now_ms=3_000)
    assert (ledger.rows["t1"]["state"], ledger.rows["t1"]["claimed_by"]) == ("done", agent.name)
    assert runtime.spawned == [("eng", agent.name, "t1")]
    assert actions == [f"retired {agent.name}", "drained"]


def test_an_open_task_closed_done_during_a_tick_spawns_no_agent(store):
    ledger, runtime = DoneMidTick([{"id": "t1", "lane": "eng"}, {"id": "t2", "lane": "eng"}]), FakeRuntime()
    ledger.closing = lambda: ledger.rows["t1"].update(state="done", done=True)
    actions = tick("sw", store, ledger, runtime, now_ms=1_000)
    assert (ledger.rows["t1"]["state"], ledger.rows["t1"]["claimed_by"]) == ("done", "")
    assert store.claimant("sw", "t1") is None
    assert runtime.spawned == [("eng", "engineer@a1b2c3-0002", "t2")]
    assert [a.task for a in workers(store)] == ["t2"]
    assert actions == [
        "spawned master master@a1b2c3-0001",
        "task t1 is done on the ledger, not claimed",
        "spawned engineer@a1b2c3-0002 for t2",
    ]


@pytest.mark.parametrize("closed_mid_tick", [True, False])
def test_a_lost_agents_open_items_follow_the_live_reopen_result(store, closed_mid_tick):
    from scripts.inbox.store import InboxStore

    ledger, runtime = DoneMidTick([{"id": "t1", "lane": "eng"}]), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    store.update("sw", state="paused")
    inbox = InboxStore(store.redis)
    item = inbox.send("master@a1b2c3-0001", "engineer@a1b2c3-0001", "your pull request has a red check")
    runtime.live.discard("engineer@a1b2c3-0001")
    if closed_mid_tick:
        ledger.closing = lambda: ledger.rows["t1"].update(state="done", done=True)
    actions = tick("sw", store, ledger, runtime, now_ms=2_000 + STARTUP_GRACE_MS)
    moved = inbox.get(item.id)
    if closed_mid_tick:
        assert ledger.rows["t1"]["state"] == "done"
        assert (moved.address, moved.state) == ("engineer@a1b2c3-0001", "cancelled")
        assert inbox.history(item.id)[-1]["reason"] == "cancelled: engineer@a1b2c3-0001 stopped before closing it"
        assert inbox.inbox("eng-1@sw") == []
        assert actions == ["lost engineer@a1b2c3-0001"]
    else:
        assert (ledger.rows["t1"]["state"], ledger.rows["t1"]["claimed_by"]) == ("open", "")
        assert (moved.address, moved.state) == ("eng-1@sw", "pending")
        assert actions == ["lost engineer@a1b2c3-0001, task t1 reopened"]


@pytest.mark.parametrize("closed_mid_tick", [True, False])
def test_a_stalled_agents_open_items_follow_the_live_reopen_result(store, closed_mid_tick):
    from scripts.inbox.store import InboxStore
    from scripts.swarm.tick import IDLE_KILL_TICKS

    ledger, runtime = DoneMidTick([{"id": "t1", "lane": "eng"}]), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    store.update("sw", state="paused")
    inbox = InboxStore(store.redis)
    item = inbox.send("master@a1b2c3-0001", "engineer@a1b2c3-0001", "your pull request has a red check")
    runtime.statuses["engineer@a1b2c3-0001"] = "idle"
    idle_for(store, ledger, runtime, IDLE_KILL_TICKS - 1, start=2_000)
    if closed_mid_tick:
        ledger.closing = lambda: ledger.rows["t1"].update(state="done", done=True)
    actions = tick("sw", store, ledger, runtime, now_ms=2_000 + IDLE_KILL_TICKS * 60_000)
    moved = inbox.get(item.id)
    assert runtime.killed == ["engineer@a1b2c3-0001"]
    if closed_mid_tick:
        assert ledger.rows["t1"]["state"] == "done"
        assert (moved.address, moved.state) == ("engineer@a1b2c3-0001", "cancelled")
        assert actions == ["stalled engineer@a1b2c3-0001"]
    else:
        assert (ledger.rows["t1"]["state"], ledger.rows["t1"]["claimed_by"]) == ("open", "")
        assert (moved.address, moved.state) == ("eng-1@sw", "pending")
        assert actions == ["stalled engineer@a1b2c3-0001, task t1 reopened"]


def test_a_task_closed_done_during_a_tick_is_not_blocked_by_the_claim_cap(store):
    ledger, runtime = DoneMidTick([{"id": "t1", "lane": "eng"}]), FakeRuntime()
    for _ in range(3):
        store.count_claim("sw", "t1")
    ledger.closing = lambda: ledger.rows["t1"].update(state="done", done=True)
    actions = tick("sw", store, ledger, runtime, now_ms=1_000)
    assert (ledger.rows["t1"]["state"], runtime.spawned) == ("done", [])
    assert actions == ["spawned master master@a1b2c3-0001", "task t1 is done on the ledger, not claimed", "drained"]


def test_a_claimed_task_without_an_agent_is_reopened(store):
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    store.update("sw", state="paused")
    ledger.rows["t1"].update(state="claimed", claimed_by="engineer@a1b2c3-0009")
    actions = tick("sw", store, ledger, runtime, now_ms=1_000)
    assert ledger.rows["t1"]["state"] == "open"
    assert ledger.rows["t1"]["claimed_by"] == ""
    assert "task t1 had no agent, reopened" in actions


def test_a_task_held_by_a_known_agent_is_not_reopened_as_an_orphan(store):
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    store.update("sw", state="paused")
    store.put_agent("sw", AgentRecord("engineer@a1b2c3-0009", "eng", "t1", started_at=1_000))
    runtime.live.add("engineer@a1b2c3-0009")
    ledger.rows["t1"].update(state="claimed", claimed_by="engineer@a1b2c3-0009")
    actions = tick("sw", store, ledger, runtime, now_ms=1_000)
    assert (ledger.rows["t1"]["state"], ledger.rows["t1"]["claimed_by"]) == ("claimed", "engineer@a1b2c3-0009")
    assert "task t1 had no agent, reopened" not in actions


def test_an_idle_agent_is_nudged_then_retired_and_its_task_reopened(store):
    from scripts.swarm.tick import IDLE_KILL_TICKS, IDLE_NUDGE_TICKS

    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    runtime.statuses["engineer@a1b2c3-0001"] = "idle"
    store.update("sw", state="paused")
    for n in range(IDLE_KILL_TICKS):
        tick("sw", store, ledger, runtime, now_ms=2_000 + n)
        if n + 1 == IDLE_NUDGE_TICKS:
            assert runtime.nudged == ["engineer@a1b2c3-0001"]
    assert runtime.killed == ["engineer@a1b2c3-0001"] and ledger.rows["t1"]["state"] == "open"


def test_the_nudge_is_skipped_while_the_pane_holds_typed_input(store):
    from scripts.swarm.tick import IDLE_NUDGE_TICKS

    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    store.update("sw", state="paused")
    runtime.statuses["engineer@a1b2c3-0001"] = "idle"
    runtime.typed["engineer@a1b2c3-0001"] = "wait, before you"
    for n in range(IDLE_NUDGE_TICKS + 2):
        tick("sw", store, ledger, runtime, now_ms=2_000 + n)
    assert runtime.nudged == [] and workers(store)[0].idle_ticks == 0
    runtime.typed.clear()
    for n in range(IDLE_NUDGE_TICKS):
        tick("sw", store, ledger, runtime, now_ms=3_000 + n)
    assert runtime.nudged == ["engineer@a1b2c3-0001"]


def test_the_nudge_is_skipped_inside_the_quiet_window_after_an_operator_prompt(store):
    from scripts.inbox import wake
    from scripts.swarm import idle
    from scripts.swarm.tick import IDLE_NUDGE_TICKS

    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    store.update("sw", state="paused")
    runtime.statuses["engineer@a1b2c3-0001"] = "idle"
    idle.prompted(store.redis, "sw", "engineer@a1b2c3-0001", 2_000)
    quiet = wake.DEFAULT_QUIET_S * 1000
    for n in range(IDLE_NUDGE_TICKS + 2):
        tick("sw", store, ledger, runtime, now_ms=2_000 + quiet - 10 + n)
    assert runtime.nudged == []
    for n in range(IDLE_NUDGE_TICKS):
        tick("sw", store, ledger, runtime, now_ms=2_000 + quiet + n)
    assert runtime.nudged == ["engineer@a1b2c3-0001"]


def test_the_nudge_says_to_answer_with_the_swarm_commands_never_in_the_terminal():
    from scripts.swarm.tick import NUDGE

    assert "agentihooks msg reply" in NUDGE and "never as text in this terminal" in NUDGE


def test_each_idle_tick_is_recorded_in_the_gate_log_with_its_time_and_task(store):
    from scripts.gates import log

    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    store.update("sw", state="paused")
    tick("sw", store, ledger, runtime, now_ms=1_500)
    assert log.recent("sw") == []
    runtime.statuses["engineer@a1b2c3-0001"] = "idle"
    tick("sw", store, ledger, runtime, now_ms=2_000)
    tick("sw", store, ledger, runtime, now_ms=3_000)
    rows = [(r["gate"], r["kind"], r["agent"], r["task"], r["reason"], r["at"]) for r in log.recent("sw")]
    assert rows == [
        ("idle-ticks", "count", "engineer@a1b2c3-0001", "t1", "idle tick 1", 2_000),
        ("idle-ticks", "count", "engineer@a1b2c3-0001", "t1", "idle tick 2", 3_000),
    ]


@pytest.mark.parametrize("state", ["pending", "delivered", "read"])
def test_a_retired_stalled_agent_leaves_its_items_on_its_seat(store, state):
    from scripts.inbox.store import InboxStore
    from scripts.swarm.tick import IDLE_KILL_TICKS

    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    inbox = InboxStore(store.redis)
    item = inbox.send("ci@a1b2c3-0009", "engineer@a1b2c3-0001", "contract confirmed")
    if state != "pending":
        getattr(inbox, "deliver" if state == "delivered" else "read")(item.id, "engineer@a1b2c3-0001")
    runtime.statuses["engineer@a1b2c3-0001"] = "idle"
    store.update("sw", state="paused")
    for n in range(IDLE_KILL_TICKS):
        tick("sw", store, ledger, runtime, now_ms=2_000 + n)
    assert runtime.killed == ["engineer@a1b2c3-0001"]
    assert (inbox.get(item.id).address, inbox.get(item.id).state) == ("eng-1@sw", "pending")


def test_a_busy_turn_resets_the_idle_count(store):
    from scripts.swarm.tick import IDLE_NUDGE_TICKS

    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    for status in ["idle"] * (IDLE_NUDGE_TICKS - 1) + ["working"] + ["idle"] * (IDLE_NUDGE_TICKS - 1):
        runtime.statuses["engineer@a1b2c3-0001"] = status
        tick("sw", store, ledger, runtime, now_ms=2_000)
    assert runtime.nudged == []


def test_a_finished_agent_that_will_not_die_stays_registered(store):
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    first = workers(store)[0]
    store.put_agent("sw", AgentRecord(**{**first.__dict__, "state": "finished"}))
    runtime.stuck.add("engineer@a1b2c3-0001")
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert [a.name for a in workers(store)] == ["engineer@a1b2c3-0001"]


def test_a_drained_swarm_wakes_up_for_new_tasks(store):
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    ledger.rows["t1"]["state"] = "done"
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert store.config("sw").state == "drained"
    ledger.rows["t2"] = {"id": "t2", "lane": "eng", "state": "open", "claimed_by": "", "out_of_scope": False}
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert store.config("sw").state == "running" and runtime.spawned == [("eng", "engineer@a1b2c3-0001", "t2")]


def test_a_handed_off_task_is_reopened_and_respawned_with_the_doc(store):
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    first = workers(store)[0]
    store.put_handoff("sw", "t1", "seam 1 green, seam 2 red")
    store.put_agent("sw", AgentRecord(**{**first.__dict__, "state": "finished"}))
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert runtime.killed == ["engineer@a1b2c3-0001"]
    assert runtime.spawned[-1] == ("eng", "engineer@a1b2c3-0002", "t1")
    assert runtime.tasks[-1]["handoff"] == "seam 1 green, seam 2 red"
    assert ledger.rows["t1"]["claimed_by"] == "engineer@a1b2c3-0002"
    assert store.handoff("sw", "t1") == ""


def test_a_spawned_agent_keeps_the_model_and_effort_it_was_placed_with(store):
    tick("sw", store, tasks(("t1", "eng")), FakeRuntime(), now_ms=1_000)
    (agent,) = workers(store)
    assert (agent.model, agent.effort) == ("opus", "high")


def masters(store):
    return [a for a in store.agents("sw") if a.lane == MASTER]


def test_a_running_swarm_spawns_one_master_that_claims_no_task(store):
    ledger, runtime = tasks(("t1", "eng"), ("t2", "eng"), ("t3", "eng")), FakeRuntime()
    assert "spawned master master@a1b2c3-0001" in tick("sw", store, ledger, runtime, 1)
    tick("sw", store, ledger, runtime, 2)
    assert [name for name, _ in runtime.masters] == ["master@a1b2c3-0001"]
    assert [(a.name, a.task) for a in masters(store)] == [("master@a1b2c3-0001", MASTER)]
    assert len(runtime.spawned) == 2
    assert {r["claimed_by"] for r in ledger.rows.values()} == {"engineer@a1b2c3-0001", "engineer@a1b2c3-0002", ""}
    assert all(store.claimant("sw", t) != "master@a1b2c3-0001" for t in ledger.rows)


def test_a_paused_or_drained_swarm_keeps_its_master_and_a_stopped_one_has_none(store):
    store.update("sw", state="paused")
    runtime = FakeRuntime()
    tick("sw", store, tasks(), runtime, 1)
    assert [a.name for a in masters(store)] == ["master@a1b2c3-0001"]
    store.update("sw", state="running")
    assert "drained" in tick("sw", store, tasks(), runtime, 2)
    assert store.config("sw").state == "drained" and [a.name for a in masters(store)] == ["master@a1b2c3-0001"]
    store.update("sw", state="stopped")
    store.drop_agent("sw", "master@a1b2c3-0001")
    assert tick("sw", store, tasks(), runtime, 3) == [] and len(runtime.masters) == 1


def test_an_operator_write_to_a_stopped_swarm_starts_its_master_and_no_engineers(store):
    from scripts.inbox.store import InboxStore

    store.update("sw", state="stopped")
    InboxStore(store.redis).send("operator", "master@sw", "operator replied on the ledger")
    runtime = FakeRuntime()
    actions = tick("sw", store, tasks(("t1", "eng"), ("t2", "ci")), runtime, 1)
    assert "spawned master master@a1b2c3-0001" in actions
    assert [a.name for a in masters(store)] == ["master@a1b2c3-0001"] and workers(store) == []
    assert runtime.spawned == [] and store.config("sw").state == "paused"


def test_a_drained_swarm_without_a_master_starts_one_and_no_engineers(store):
    store.update("sw", state="drained")
    runtime = FakeRuntime()
    actions = tick("sw", store, tasks(), runtime, 1)
    assert "spawned master master@a1b2c3-0001" in actions and runtime.spawned == []
    assert store.config("sw").state == "drained"


@pytest.mark.parametrize(
    "sender, text",
    [
        ("swarm", "swarm added a follow-up on ledger sw: A message to master@sw is still unread"),
        ("engineer@a1b2c3-0002", "a question for the master"),
    ],
)
def test_only_the_operator_wakes_a_stopped_swarm(store, sender, text):
    from scripts.inbox.store import InboxStore

    store.update("sw", state="stopped")
    InboxStore(store.redis).send(sender, "master@sw", text)
    runtime = FakeRuntime()
    assert tick("sw", store, tasks(("t1", "eng")), runtime, 1) == []
    assert runtime.masters == [] and store.config("sw").state == "stopped"


@pytest.mark.parametrize("ref, wakes", [("ledger:note", True), ("swarm-control:stop", False)])
def test_an_operator_fyi_wakes_a_stopped_swarm_unless_it_is_a_control_notice(store, ref, wakes):
    from scripts.inbox.store import InboxStore

    store.update("sw", state="stopped")
    InboxStore(store.redis).send("operator", "master@sw", "for your information", ref=ref, fyi=True)
    tick("sw", store, tasks(("t1", "eng")), FakeRuntime(), 1)
    assert (store.config("sw").state == "paused") is wakes


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
    assert "lost master@a1b2c3-0001" in actions and "spawned master master@a1b2c3-0002" in actions
    assert [a.name for a in masters(store)] == ["master@a1b2c3-0002"]


def _told(inbox, item):
    return [(e.get("state"), e.get("event"), e["by"], e["reason"]) for e in inbox.history(item.id)]


def test_an_operator_line_with_no_live_master_posts_the_down_notice_and_reaches_the_new_master(store):
    from scripts.inbox.store import InboxStore
    from scripts.swarm.tick import DOWN_TOLD, REDELIVERED

    inbox = InboxStore(store.redis)
    runtime, ledger = FakeRuntime(), tasks(("t1", "eng"))
    tick("sw", store, ledger, runtime, 1)
    taken = inbox.send("operator", "master@sw", "the master launch failed, relaunch it")
    assert inbox.deliver(taken.id, "master@a1b2c3-0001")
    inbox.send("sw-eng-1", "master@sw", "status for the master")
    runtime.live.discard("master@a1b2c3-0001")
    runtime.full = True
    unaddressed = inbox.send("operator", "master@sw", "can anyone reply")
    down = 1 + STARTUP_GRACE_MS + 1
    actions = tick("sw", store, ledger, runtime, down)
    assert "master down, told the operator about 2 waiting lines" in actions
    assert ledger.notes.count(MASTER_DOWN) == 1
    told = (None, DOWN_TOLD, "swarm", MASTER_DOWN)
    assert _told(inbox, taken)[-2:] == [("pending", None, "swarm", REDELIVERED), told]
    assert _told(inbox, unaddressed) == [("pending", None, "operator", ""), told]
    assert inbox.history(unaddressed.id)[-1]["at"] == down
    tick("sw", store, ledger, runtime, down + 1)
    assert ledger.notes.count(MASTER_DOWN) == 1
    later = inbox.send("operator", "master@sw", "still nobody?")
    tick("sw", store, ledger, runtime, down + 2)
    assert ledger.notes.count(MASTER_DOWN) == 2
    runtime.full = False
    actions = tick("sw", store, ledger, runtime, down + 3)
    assert "spawned master master@a1b2c3-0002" in actions and ledger.notes.count(MASTER_DOWN) == 2
    mail = inbox.pending_mail("master@a1b2c3-0002")
    assert {i.id for i in mail if i.sender == "operator"} == {taken.id, unaddressed.id, later.id}


def test_an_operator_line_for_a_live_master_posts_no_down_notice(store):
    from scripts.inbox.store import InboxStore

    runtime, ledger = FakeRuntime(), tasks()
    tick("sw", store, ledger, runtime, 1)
    InboxStore(store.redis).send("operator", "master@sw", "how far along")
    tick("sw", store, ledger, runtime, 1 + STARTUP_GRACE_MS + 5)
    assert MASTER_DOWN not in ledger.notes


def test_a_master_still_starting_posts_no_down_notice(store):
    from scripts.inbox.store import InboxStore

    start = 10**9
    runtime, ledger = FakeRuntime(), tasks()
    tick("sw", store, ledger, runtime, start)
    runtime.live.clear()
    InboxStore(store.redis).send("operator", "master@sw", "how far along")
    tick("sw", store, ledger, runtime, start + STARTUP_GRACE_MS)
    assert MASTER_DOWN not in ledger.notes and [a.name for a in masters(store)] == ["master@a1b2c3-0001"]


def test_a_stopping_swarm_posts_no_down_notice(store):
    from scripts.inbox.store import InboxStore

    runtime, ledger = FakeRuntime(), tasks()
    tick("sw", store, ledger, runtime, 1)
    runtime.live.clear()
    store.update("sw", state="stopping")
    InboxStore(store.redis).send("operator", "master@sw", "how far along")
    tick("sw", store, ledger, runtime, 1 + STARTUP_GRACE_MS + 1)
    assert MASTER_DOWN not in ledger.notes


def test_a_finished_master_that_will_not_retire_counts_as_down(store):
    from scripts.inbox.store import InboxStore

    runtime, ledger = FakeRuntime(), tasks()
    tick("sw", store, ledger, runtime, 1)
    [boss] = masters(store)
    store.put_agent("sw", replace(boss, state="finished"))
    runtime.stuck.add(boss.name)
    InboxStore(store.redis).send("operator", "master@sw", "how far along")
    tick("sw", store, ledger, runtime, 2)
    assert MASTER_DOWN in ledger.notes


def test_an_idle_master_is_never_nudged_or_stalled(store):
    runtime = FakeRuntime()
    tick("sw", store, tasks(), runtime, 1)
    runtime.statuses["master@a1b2c3-0001"] = "idle"
    for n in range(12):
        tick("sw", store, tasks(), runtime, 2 + n)
    assert runtime.nudged == [] and [a.name for a in masters(store)] == ["master@a1b2c3-0001"]


def test_the_tick_names_a_live_masters_pane(store):
    runtime = FakeRuntime()
    tick("sw", store, tasks(), runtime, 1)
    tick("sw", store, tasks(), runtime, 2)
    assert runtime.named == ["master@a1b2c3-0001"]


def test_a_master_handoff_retires_the_old_master_and_spawns_the_next_with_the_doc(store):
    runtime = FakeRuntime()
    tick("sw", store, tasks(), runtime, 1)
    (old,) = masters(store)
    store.put_handoff("sw", MASTER, "operator wants caps at four")
    store.put_agent("sw", replace(old, state="finished"))
    actions = tick("sw", store, tasks(), runtime, 2)
    assert "retired master@a1b2c3-0001" in actions and "spawned master master@a1b2c3-0002" in actions
    assert runtime.killed == ["master@a1b2c3-0001"]
    assert runtime.masters[-1][1]["handoff"] == "operator wants caps at four"
    assert store.handoff("sw", MASTER) == ""
    assert [a.name for a in masters(store)] == ["master@a1b2c3-0002"]


def test_a_full_house_waits_for_a_slot_before_starting_the_master(store):
    assert "no session slot for the master, waiting" in tick("sw", store, tasks(), FakeRuntime(full=True), 1)
    assert masters(store) == []


def test_a_stopping_swarm_keeps_its_master_until_the_last_worker_leaves(store, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_MASTER_RETIRE_HANDOFF_MINUTES", "0")
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, 1)
    store.update("sw", state="stopping")
    tick("sw", store, ledger, runtime, 2)
    assert [a.name for a in masters(store)] == ["master@a1b2c3-0001"]
    store.put_agent("sw", replace(workers(store)[0], state="finished"))
    actions = tick("sw", store, ledger, runtime, 3)
    assert "retired master@a1b2c3-0001" in actions and actions[-1] == "stopped"
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


def test_an_urgent_ready_task_is_claimed_ahead_of_an_older_normal_one(store):
    store.update("sw", max_eng=1)
    ledger = FakeLedger([{"id": "t1"}, {"id": "t2", "rank": "urgent"}])
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert spawned_ids(runtime) == ["t2"]


def test_a_blocked_urgent_task_is_skipped_for_a_ready_normal_one(store):
    store.update("sw", max_eng=1)
    ledger = FakeLedger(
        [{"id": "t0", "state": "blocked"}, {"id": "t1", "rank": "urgent", "depends_on": ["t0"]}, {"id": "t2"}]
    )
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert spawned_ids(runtime) == ["t2"] and ledger.rows["t1"]["state"] == "open"


def test_an_urgent_task_never_takes_a_territory_an_active_claim_holds(store):
    ledger = FakeLedger([{"id": "t1", "territory": ["hooks"]}])
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    ledger.rows["t2"] = {**ledger.rows["t1"], "id": "t2", "state": "open", "claimed_by": "", "rank": "urgent"}
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert spawned_ids(runtime) == ["t1"] and ledger.rows["t1"]["state"] == "claimed"


def test_an_urgent_task_wins_a_shared_territory_over_an_earlier_normal_one(store):
    ledger = FakeLedger([{"id": "t1", "territory": ["hooks"]}, {"id": "t2", "rank": "urgent", "territory": ["hooks"]}])
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert spawned_ids(runtime) == ["t2"]


def test_equal_ranks_keep_ledger_order_and_ranks_order_the_rest(store):
    store.update("sw", max_eng=6)
    ledger = FakeLedger(
        [
            {"id": "t1", "rank": "low"},
            {"id": "t2"},
            {"id": "t3", "rank": "high"},
            {"id": "t4", "rank": "normal"},
            {"id": "t5", "rank": "high"},
            {"id": "t6", "rank": "urgent"},
        ]
    )
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert spawned_ids(runtime) == ["t6", "t3", "t5", "t2", "t4", "t1"]


def test_a_rank_change_applies_on_the_next_tick(store):
    store.update("sw", max_eng=1)
    ledger = FakeLedger([{"id": "t1"}, {"id": "t2"}, {"id": "t3"}])
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert spawned_ids(runtime) == ["t1"]
    ledger.rows["t1"]["state"] = "done"
    store.put_agent("sw", replace(workers(store)[0], state="finished"))
    ledger.rows["t3"]["rank"] = "high"
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert spawned_ids(runtime)[1:] == ["t3"]


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
        "master@a1b2c3-0001": "master@sw",
        "engineer@a1b2c3-0001": "eng-1@sw",
        "engineer@a1b2c3-0002": "eng-2@sw",
        "ci@a1b2c3-0001": "ci-1@sw",
    }
    assert store.seats.occupant("eng-2@sw").occupant == "engineer@a1b2c3-0002"


def test_a_freed_slot_is_taken_by_the_next_agent_with_a_new_generation(store):
    ledger, runtime = tasks(("t1", "eng"), ("t2", "eng"), ("t3", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    ledger.rows["t1"]["state"] = "done"
    first = workers(store)[0]
    store.put_agent("sw", replace(first, state="finished"))
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert seats_of(store)["engineer@a1b2c3-0003"] == "eng-1@sw"
    assert [(e["generation"], e["occupant"]) for e in store.seats.history("eng-1@sw")] == [
        (1, "engineer@a1b2c3-0001"),
        (2, "engineer@a1b2c3-0003"),
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
    assert runtime.spawned[-1] == ("eng", "engineer@a1b2c3-0003", "t2")
    assert seats_of(store)["engineer@a1b2c3-0003"] == "eng-2@sw"
    assert store.handoff_seat("sw", "t2") == ""


def test_a_master_handoff_keeps_the_master_seat_and_bumps_its_generation(store):
    runtime = FakeRuntime()
    tick("sw", store, tasks(), runtime, 1)
    (old,) = masters(store)
    store.put_handoff("sw", MASTER, "doc", seat=old.seat)
    store.put_agent("sw", replace(old, state="finished"))
    tick("sw", store, tasks(), runtime, 2)
    assert store.seats.occupant("master@sw").occupant == "master@a1b2c3-0002"
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
    assert (
        runtime.spawned[-1] == ("eng", "engineer@a1b2c3-0003", "t2")
        and seats_of(store)["engineer@a1b2c3-0003"] == "eng-2@sw"
    )


def test_a_failed_spawn_leaves_the_seat_with_a_new_generation_and_the_task_open(store):
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime(fail=True)
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert ledger.rows["t1"]["state"] == "open"
    assert [e["occupant"] for e in store.seats.history("eng-1@sw")] == ["engineer@a1b2c3-0001"]


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


def test_overflow_on_codex_in_the_window_leaves_the_next_free_spawn_to_the_share(store, monkeypatch):
    from scripts import agent_choice

    now = 10 * 3_600_000
    rows = [
        *[(f"s{i}", "claude", "share", now - i * 1000) for i in range(1, 3)],
        *[(f"o{i}", "codex", "overflow", now - i * 1000) for i in range(1, 4)],
        ("f1", "codex", "forced", now - 9000),
        ("old", "codex", "share", now - 7 * 3_600_000),
        *[(f"s{i}", "claude", "share", now - i * 1000) for i in range(3, 5)],
    ]
    for name, harness, choice, at in rows:
        store.put_agent("sw", AgentRecord(name, "eng", f"x-{name}", harness=harness, started_at=at, choice=choice))
        store.drop_agent("sw", name, at=at)
        store.count_spawn("sw", harness)
    rt = FakeRuntime()
    tick("sw", store, FakeLedger([{"id": "t1"}]), rt, now)
    assert rt.spawns_seen == [{"claude": 4}]
    monkeypatch.setattr(agent_choice, "at_cap", lambda agent, environ: False)
    monkeypatch.setattr(agent_choice, "codex_week_left", lambda environ: 90.0)
    assert agent_choice.choose_shared("", {}, rt.spawns_seen[0], 20, 5, choose=lambda r, e: ("claude", "priority")) == (
        "codex",
        "codex share 0/4 below 20%",
    )


def _conversations(store):
    return {a.name: a.conversation_id for a in store.agents("sw")}


def test_a_spawned_agent_gets_the_conversation_id_herdr_reports_for_its_pane(store):
    rt = FakeRuntime()
    rt.conversation_ids = {"w1:p1": "5c90d80c", "w1:m1": "15e33356"}
    tick("sw", store, FakeLedger([{"id": "t1"}]), rt, 1000)
    assert _conversations(store) == {"engineer@a1b2c3-0001": "5c90d80c", "master@a1b2c3-0001": "15e33356"}


def test_each_tick_refreshes_the_conversation_id_and_stores_unknown_as_empty(store):
    rt = FakeRuntime()
    ledger = FakeLedger([{"id": "t1"}, {"id": "t2"}])
    tick("sw", store, ledger, rt, 1000)
    assert _conversations(store) == {"engineer@a1b2c3-0001": "", "engineer@a1b2c3-0002": "", "master@a1b2c3-0001": ""}
    rt.conversation_ids = {"w1:p1": "first", "w1:p2": "other"}
    tick("sw", store, ledger, rt, 2000)
    assert _conversations(store) == {
        "engineer@a1b2c3-0001": "first",
        "engineer@a1b2c3-0002": "other",
        "master@a1b2c3-0001": "",
    }
    rt.conversation_ids = {"w1:p1": "resumed", "w1:p2": ""}
    tick("sw", store, ledger, rt, 3000)
    assert _conversations(store) == {
        "engineer@a1b2c3-0001": "resumed",
        "engineer@a1b2c3-0002": "",
        "master@a1b2c3-0001": "",
    }
    del rt.conversation_ids["w1:p1"]
    tick("sw", store, ledger, rt, 4000)
    assert _conversations(store)["engineer@a1b2c3-0001"] == ""


def test_a_tick_without_an_answer_from_herdr_keeps_the_known_conversation_ids(store):
    rt = FakeRuntime()
    rt.conversation_ids = {"w1:p1": "5c90d80c"}
    ledger = FakeLedger([{"id": "t1"}])
    tick("sw", store, ledger, rt, 1000)
    rt.conversation_ids = None
    tick("sw", store, ledger, rt, 2000)
    assert _conversations(store)["engineer@a1b2c3-0001"] == "5c90d80c"


def test_a_model_switch_the_running_session_reports_reaches_its_agent_record_on_the_next_tick(store):
    from hooks.context import swarm_heartbeat

    ledger, rt = FakeLedger([{"id": "t1"}]), FakeRuntime()
    tick("sw", store, ledger, rt, 1_000)
    name = "engineer@a1b2c3-0001"
    swarm = {"AGENTIHOOKS_SWARM": "sw", "AGENTIHOOKS_AGENT_NAME": name}
    assert swarm_heartbeat.report("claude-sonnet-5-5", "low", environ=swarm, redis=store.redis, now_ms=2_000) is True
    tick("sw", store, ledger, rt, 3_000)
    agent = next(a for a in store.agents("sw") if a.name == name)
    assert (agent.model, agent.effort, agent.model_source, agent.model_confidence) == (
        "claude-sonnet-5-5",
        "low",
        "session",
        None,
    )


def idle_for(store, ledger, runtime, ticks, start):
    for n in range(ticks):
        tick("sw", store, ledger, runtime, now_ms=start + n * 60_000)


def test_an_idle_agent_under_a_declared_wait_is_never_nudged_or_retired(store):
    from scripts.swarm import idle
    from scripts.swarm.tick import IDLE_KILL_TICKS

    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
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
    tick("sw", store, ledger, runtime, now_ms=1_000)
    store.update("sw", state="paused")
    runtime.statuses["engineer@a1b2c3-0001"] = "idle"
    for n in range(IDLE_KILL_TICKS + 2):
        idle.beat(store.redis, "sw", "engineer@a1b2c3-0001", idle.WORKING, 2_000 + n * 60_000)
        tick("sw", store, ledger, runtime, now_ms=2_000 + n * 60_000)
    assert runtime.nudged == [] and runtime.killed == []


def test_a_stalled_agents_open_messages_move_to_its_seat_for_the_next_engineer(store):
    from scripts.inbox.store import InboxStore
    from scripts.swarm.tick import IDLE_KILL_TICKS

    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    store.update("sw", state="paused")
    inbox = InboxStore(store.redis)
    open_item = inbox.send("master@a1b2c3-0001", "engineer@a1b2c3-0001", "your pull request has a red check")
    closed = inbox.send("master@a1b2c3-0001", "engineer@a1b2c3-0001", "old news")
    inbox.close(closed.id, "engineer@a1b2c3-0001", "done", "handled the request")
    runtime.statuses["engineer@a1b2c3-0001"] = "idle"
    idle_for(store, ledger, runtime, IDLE_KILL_TICKS, start=2_000)
    assert runtime.killed == ["engineer@a1b2c3-0001"]
    assert [(i.id, i.state) for i in inbox.inbox("eng-1@sw")] == [(open_item.id, "pending")]
    assert [i.id for i in inbox.inbox("engineer@a1b2c3-0001")] == [closed.id]


@pytest.mark.parametrize("drifted", [True, False])
def test_the_tick_restores_the_approved_codex_hook_order_and_journals_it(store, drifted):
    import json

    from scripts.targets.codex_target import codex_home

    home = codex_home()
    home.mkdir(parents=True, exist_ok=True)
    ours = {"hooks": [{"type": "command", "command": str(home / "agentihooks-hook.sh")}]}
    herdr = {"hooks": [{"command": "bash herdr-agent-state.sh session", "timeout": 10, "type": "command"}]}
    groups = [herdr, ours] if drifted else [ours, herdr]
    (home / "hooks.json").write_text(json.dumps({"hooks": {"SessionStart": groups, "Stop": groups}}, indent=2))

    actions = tick("sw", store, tasks(("t1", "eng")), FakeRuntime(), now_ms=1_000)

    line = f"restored the approved Codex hook order in {home / 'hooks.json'}: SessionStart, Stop"
    assert (line in actions) is drifted
    assert json.loads((home / "hooks.json").read_text())["hooks"]["SessionStart"][0] == ours
