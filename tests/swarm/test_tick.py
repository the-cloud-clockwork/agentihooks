import subprocess
from dataclasses import replace

import pytest

from scripts.swarm import stack
from scripts.swarm.store import MASTER, AgentRecord, RedisStore, SwarmConfig, SwarmError
from scripts.swarm.tick import MASTER_DOWN, STARTUP_GRACE_MS, Placed, SpawnError, tick


def check_update_guards(if_state, if_match):
    import sys
    from unittest.mock import patch

    from scripts.swarm_ledger import HERE

    with patch.object(sys, "path", [str(HERE), *sys.path]):
        from scripts.swarm_ledger import ledger_tasks

        guard = {"if_state": list(if_state)} if if_state else {}
        if if_match is not None:
            guard["if_match"] = if_match
        ledger_tasks.check_guards(guard)


class FakeLedger:
    def __init__(self, tasks):
        self.rows = {
            t["id"]: {"state": "open", "claimed_by": "", "out_of_scope": False, "lane": "eng", **t} for t in tasks
        }
        self.notes = []
        self.swarm_sized = []
        self.ranks, self.comments = [], []

    def tasks(self, slug):
        return list(self.rows.values())

    def task(self, slug, task_id):
        return self.rows.get(task_id)

    def events(self, slug):
        return []

    def state(self, slug):
        log, tasks = getattr(self, "log", []), self.tasks(slug)
        stamps = {f"tasks/{t['id']}/rank": {"by": "operator"} for t in tasks if "rank" in t}
        return {
            "overview": "Project intent",
            "tasks": tasks,
            "phases": getattr(self, "phases", []),
            "_meta": {"rev": len(log), "events": log, "stamps": getattr(self, "stamps", stamps)},
            "followups": [],
        }

    def hierarchy(self, slug):
        tasks = self.tasks(slug)
        return [
            row
            for phase in getattr(self, "phases", [])
            for row in [
                {"node": f"phases/{phase['id']}", "kind": "phase", "depth": 0, "state": "open"},
                *(
                    {
                        "node": f"tasks/{t['id']}",
                        "kind": "task",
                        "depth": 1,
                        "state": "out_of_scope" if t.get("out_of_scope") else t["state"],
                    }
                    for t in tasks
                    if t.get("phase") == phase["id"]
                ),
            ]
        ]

    def update_task(self, slug, task_id, fields, by="swarm", if_state=(), if_match=None):
        check_update_guards(if_state, if_match)
        assert slug == "sw"
        row = self.rows[task_id]
        if any(
            row.get(key, 0 if key == "claim_generation" else "") != value for key, value in (if_match or {}).items()
        ):
            return dict(row)
        if not if_state or row["state"] in if_state:
            if "claimed_by" in fields and fields["claimed_by"] != row.get("claimed_by"):
                row["claim_generation"] = row.get("claim_generation", 0) + 1
            row.update(fields)
        return dict(row)

    def notify(self, slug, text):
        assert slug == "sw"
        self.notes.append(text)

    def mark_swarm(self, slug):
        self.swarm_sized.append(slug)

    def binned(self, slug):
        return slug in getattr(self, "bin", set())

    def closed(self, slug):
        return bool(self.state(slug).get("closed_at"))

    def rank_task(self, slug, task_id, rank, by, if_owned=False):
        self.ranks.append((task_id, rank, by))
        return {"id": task_id, "rank": rank}

    def comment(self, slug, task_id, text, by):
        self.comments.append((task_id, text, by))


class FakeRuntime:
    def __init__(self, fail=False, full=False, crash=None):
        self.live, self.spawned, self.killed, self.closed, self.nudged = set(), [], [], [], []
        self.tasks, self.masters, self.harness = [], [], "claude"
        self.fail, self.full, self.crash, self.statuses, self.stuck = fail, full, crash, {}, set()
        self.conversation_ids, self.named, self.closed_spaces, self.typed = {}, [], [], {}
        self.capacity_for = []
        self.homes, self.duplicates, self.refusals, self.reaped = {}, set(), {}, []
        self.nudge_refusals, self.nudge_calls = {}, []

    def has_capacity(self, config):
        self.capacity_for.append(config.slug)
        return not self.full

    def spawn(self, config, lane, name, task):
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

    def retire(self, agent, homes=()):
        self.homes[agent.name] = list(homes)
        if agent.name in self.stuck:
            return False
        if agent.name in self.live:
            self.killed.append(agent.name)
            if agent.name not in self.duplicates:
                self.live.discard(agent.name)
        self.closed.append(agent.pane_id)
        return True

    def refusal(self, agent):
        return self.refusals.get(agent.name, {"process": 0, "refusal": "stuck"})

    def reap_name(self, name):
        self.reaped.append(name)
        if name in self.stuck:
            return False
        self.live.discard(name)
        return True

    def status(self, agent):
        return self.statuses.get(agent.name, "working")

    def observe(self, agent):
        from scripts.swarm.pane import PaneObservation

        return PaneObservation(self.status(agent), typed=self.typed.get(agent.name, ""))

    def nudge(self, agent, text):
        self.nudged.append(agent.name)
        self.nudge_calls.append((agent.idle_ticks, text))
        return self.nudge_refusals.get(agent.name)

    def name_pane(self, agent):
        self.named.append(agent.name)
        return False

    def conversations(self):
        return None if self.conversation_ids is None else dict(self.conversation_ids)

    def close_space(self, config):
        self.closed_spaces.append(config.slug)
        return True

    def pane_tail(self, pane_id, lines):
        return ""

    def launched_pane(self, pane_id, name):
        return True

    def close_pane(self, pane_id):
        self.closed.append(pane_id)


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
        ("ci", "ci@a1b2c3-0001", "t4"),
        ("eng", "engineer@a1b2c3-0002", "t2"),
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


class RefusingRuntime(FakeRuntime):
    def placement_refusal(self, lane, task):
        return "no headless hive for eng lists the repo /repo" if task.get("id") == "t1" else ""


def test_a_placement_wait_is_logged_in_the_tick_output(store):
    ledger, runtime = tasks(("t1", "eng"), ("t2", "eng"), ("t3", "eng")), RefusingRuntime()
    actions = tick("sw", store, ledger, runtime, now_ms=1_000)
    assert "task t1 waits: no headless hive for eng lists the repo /repo" in actions
    assert [task for _, _, task in runtime.spawned] == ["t2", "t3"]


@pytest.mark.parametrize("lost_ledger", ["missing", "retained", False])
@pytest.mark.parametrize("pr_url, expected", [("", "claimed"), ("https://github.com/o/r/pull/1", "pr")])
def test_a_live_owner_recovers_its_missing_lease_without_a_duplicate_launch(store, lost_ledger, pr_url, expected):
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    owner = next(a for a in store.agents("sw") if a.task == "t1")
    ledger.rows["t1"].update(pr_url=pr_url, state=expected)
    store.release("sw", "t1", owner.name)
    if lost_ledger:
        ledger.rows["t1"].update(state="open")
        if lost_ledger == "missing":
            ledger.rows["t1"]["claimed_by"] = ""
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert len(runtime.spawned) == 1
    assert store.claimant("sw", "t1") == owner.name
    assert ledger.rows["t1"]["claimed_by"] == owner.name
    assert ledger.rows["t1"]["state"] == expected
    assert store.redis.pttl(store.key("sw", "claim", "t1")) > 0


@pytest.mark.parametrize("state", ["claimed", "pr"])
def test_a_live_owner_recovers_a_missing_claimant_on_an_active_ledger_row(store, state):
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    owner = next(a for a in store.agents("sw") if a.task == "t1")
    ledger.rows["t1"].update(state=state, claimed_by="")
    store.release("sw", "t1", owner.name)
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert ledger.rows["t1"]["claimed_by"] == owner.name
    assert ledger.rows["t1"]["state"] == state
    assert store.claimant("sw", "t1") == owner.name
    assert len(runtime.spawned) == 1


def test_a_stale_live_agent_cannot_restore_a_lease_over_the_ledger_successor(store):
    from scripts.swarm.store import AgentRecord

    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    owner = next(a for a in store.agents("sw") if a.task == "t1")
    store.release("sw", "t1", owner.name)
    stale = AgentRecord("a-stale-owner", "eng", "t1", pane_id="old-pane", started_at=100)
    store.put_agent("sw", stale)
    runtime.live.add(stale.name)
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert ledger.rows["t1"]["claimed_by"] == owner.name
    assert store.claimant("sw", "t1") == owner.name
    assert len(runtime.spawned) == 1


def test_a_stale_predecessor_does_not_hold_the_ci_lane_a_successor_owns(store):
    from scripts.swarm.store import AgentRecord

    store.update("sw", max_eng=0, max_ci=2)
    ledger = FakeLedger(
        [
            {"id": "owned", "lane": "ci", "state": "claimed", "claimed_by": "ci-successor"},
            {"id": "urgent", "lane": "ci", "rank": "urgent"},
        ]
    )
    runtime = FakeRuntime()
    for name, seat in [("ci-predecessor", "ci-1@sw"), ("ci-successor", "ci-2@sw")]:
        store.put_agent("sw", AgentRecord(name, "ci", "owned", seat=seat, started_at=1_000))
        store.seats.occupy(seat, name, 1_000)
        runtime.live.add(name)
    store.claim("sw", "owned", "ci-successor", 60_000)
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert [task for _, _, task in runtime.spawned] == ["urgent"]
    assert ledger.rows["owned"]["claimed_by"] == "ci-successor"
    assert store.claimant("sw", "owned") == "ci-successor"


def test_ownership_repair_keeps_a_newer_ledger_state_and_claim(store, monkeypatch):
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    ledger.rows["t1"].update(state="open", claimed_by="")
    update = ledger.update_task

    def blocked(slug, task_id, fields, by="swarm", if_state=(), if_match=None):
        ledger.rows[task_id]["state"] = "blocked"
        return update(slug, task_id, fields, by, if_state, if_match)

    monkeypatch.setattr(ledger, "update_task", blocked)
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert ledger.rows["t1"]["state"] == "blocked"
    assert ledger.rows["t1"]["claimed_by"] == ""


def test_a_finished_predecessor_cannot_hold_reopened_work_when_retirement_is_stuck(store):
    from dataclasses import replace

    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    owner = next(a for a in store.agents("sw") if a.task == "t1")
    store.put_agent("sw", replace(owner, state="finished"))
    store.release("sw", "t1", owner.name)
    runtime.stuck.add(owner.name)
    ledger.rows["t1"].update(state="open", claimed_by="")
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert len(runtime.spawned) == 2
    assert ledger.rows["t1"]["claimed_by"] != owner.name


def test_a_legacy_live_owner_recovers_without_a_ledger_generation(store):
    from scripts.swarm.store import AgentRecord

    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    store.put_agent("sw", AgentRecord("legacy-owner", "eng", "t1", started_at=1_000))
    runtime.live.add("legacy-owner")
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert ledger.rows["t1"]["claimed_by"] == "legacy-owner"
    assert ledger.rows["t1"]["claim_generation"] == 1
    assert ledger.rows["t1"]["state"] == "claimed"
    assert runtime.spawned == []


def test_an_open_row_with_a_dead_claimant_can_be_claimed_from_its_exact_snapshot(store):
    ledger = FakeLedger([{"id": "t1", "claimed_by": "gone-owner"}])
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert [task for _, _, task in runtime.spawned] == ["t1"]
    assert ledger.rows["t1"]["claimed_by"] != "gone-owner"


def test_ownership_repair_keeps_a_successor_claim_created_at_the_write_boundary(store, monkeypatch):
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    ledger.rows["t1"].update(state="open", claimed_by="")
    update = ledger.update_task

    def successor(slug, task_id, fields, by="swarm", if_state=(), if_match=None):
        ledger.rows[task_id].update(claimed_by="successor", claim_generation=9)
        return update(slug, task_id, fields, by, if_state, if_match)

    monkeypatch.setattr(ledger, "update_task", successor)
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert ledger.rows["t1"]["claimed_by"] == "successor"
    assert ledger.rows["t1"]["claim_generation"] == 9


@pytest.mark.parametrize("registered", [True, False])
def test_a_live_owner_that_appears_during_placement_is_never_launched_over(store, monkeypatch, registered):
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    from scripts.swarm.store import AgentRecord

    def admitted(*args):
        if registered:
            store.put_agent("sw", AgentRecord("existing-owner", "eng", "t1", started_at=1_000))
        runtime.live.add("existing-owner")
        ledger.rows["t1"]["claimed_by"] = "existing-owner"
        return ""

    monkeypatch.setattr("scripts.swarm.tick._held_back", admitted)
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert runtime.spawned == []
    assert ledger.rows["t1"]["claimed_by"] == "existing-owner"


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


@pytest.mark.parametrize("error", [SpawnError("profile canary timeout"), OSError("worktree timer")])
def test_failed_launches_do_not_consume_lives_and_preserve_each_error(store, error):
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime(crash=error)
    for at in range(1_000, 6_000, 1_000):
        tick("sw", store, ledger, runtime, now_ms=at)
    assert store.claims("sw", "t1") == 0
    launches = store.launches("sw")
    assert len(launches) == 5
    assert {(row["task"], row["state"], row["error"]) for row in launches} == {("t1", "failed", str(error))}
    runtime.crash = None
    tick("sw", store, ledger, runtime, now_ms=6_000)
    assert len(runtime.spawned) == 1
    assert store.claims("sw", "t1") == 1
    assert [row["state"] for row in store.launches("sw")].count("started") == 1


@pytest.mark.parametrize(
    ("marked", "state_by", "after"),
    [
        ("watchdog", "swarm", "watchdog"),
        ("park", "swarm", "park"),
        ("", "master@abc123-0001", "master"),
        ("", "engineer@abc123-0002", ""),
        ("", "swarm", ""),
    ],
)
def test_a_started_launch_records_why_its_task_was_open(store, marked, state_by, after):
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    ledger.stamps = {"tasks/t1/state": {"by": state_by}}
    if marked:
        store.mark_restart("sw", "t1", marked)
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert [(row["state"], row["after"]) for row in store.launches("sw")] == [("started", after)]
    assert store.take_restart("sw", "t1") == ""


def test_a_failed_launch_keeps_the_restart_mark_for_the_next_start(store):
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime(crash=OSError("worktree timer"))
    store.mark_restart("sw", "t1", "park")
    tick("sw", store, ledger, runtime, now_ms=1_000)
    runtime.crash = None
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert sorted((row["state"], row["after"]) for row in store.launches("sw")) == [("failed", ""), ("started", "park")]


@pytest.mark.parametrize(
    ("pr_url", "read", "head"),
    [
        ("", "abc123", ""),
        ("https://github.com/o/r/pull/7", "abc123", "abc123"),
        ("https://github.com/o/r/pull/7", "", None),
    ],
)
def test_a_started_launch_records_the_pull_request_head_it_started_on(store, monkeypatch, pr_url, read, head):
    from scripts.swarm import ledger_events

    seen = []
    monkeypatch.setattr(ledger_events, "head", lambda url: seen.append(url) or read)
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    if pr_url:
        ledger.rows["t1"]["pr_url"] = pr_url
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert [(row["state"], row["head"]) for row in store.launches("sw")] == [("started", head)]
    assert seen == ([pr_url] if pr_url else [])


class FailingFor(FakeRuntime):
    def __init__(self, *failing):
        super().__init__()
        self.failing, self.tried = set(failing), []

    def spawn(self, config, lane, name, task):
        if lane != MASTER:
            self.tried.append(task["id"])
        if task["id"] in self.failing:
            raise SpawnError("MCP_KEY_GATEWAY is unset")
        return super().spawn(config, lane, name, task)


def test_a_failing_launch_does_not_stop_the_spawns_behind_it(store):
    ledger, runtime = tasks(("t1", "eng"), ("t2", "eng"), ("t3", "ci")), FailingFor("t1")
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert [task for _, _, task in runtime.spawned] == ["t3", "t2"]
    assert ledger.rows["t1"]["state"] == "open"


def test_a_task_whose_launch_failed_takes_only_a_slot_left_over(store):
    ledger, runtime = tasks(("t1", "eng"), ("t2", "eng"), ("t3", "eng")), FailingFor("t1")
    tick("sw", store, ledger, runtime, now_ms=1_000)
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert runtime.tried == ["t1", "t2", "t3"]
    assert [task for _, _, task in runtime.spawned] == ["t2", "t3"]


def test_rank_precedes_launch_failures_when_selecting_a_lane_task(store):
    store.update("sw", autonomy="assist")
    store.update("sw", max_eng=1)
    ledger = FakeLedger(
        [
            {"id": "normal"},
            {"id": "high", "rank": "high"},
            {"id": "urgent", "rank": "urgent"},
        ]
    )
    store.note_launch_failure("sw", "urgent", "canary timeout")
    store.note_launch_failure("sw", "high", "canary timeout")
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert spawned_ids(runtime) == ["urgent"]
    store.update("sw", max_eng=2)
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert spawned_ids(runtime) == ["urgent", "high"]


@pytest.mark.parametrize(
    ("error", "failure", "launch"),
    [
        (SpawnError("no claude account has placeable quota seats", "unavailable"), "", "waiting"),
        (SpawnError("profile canary timeout", "refused"), "profile canary timeout", "failed"),
        (OSError("worktree timer"), "worktree timer", "failed"),
    ],
)
def test_quota_seat_refusal_does_not_mark_a_task_launch_failure(store, error, failure, launch):
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime(crash=error)
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert store.launch_failure("sw", "t1") == failure
    assert ledger.rows["t1"]["state"] == "open"
    assert store.claimant("sw", "t1") is None
    assert store.claims("sw", "t1") == 0
    assert [(row["state"], row["error"]) for row in store.launches("sw")] == [(launch, str(error))]


def test_a_launch_is_pending_while_the_runtime_spawns_it(store):
    ledger, runtime, seen = tasks(("t1", "eng")), FakeRuntime(), []
    spawn = runtime.spawn

    def watching(*args, **kwargs):
        seen.extend(row["state"] for row in store.launches("sw"))
        return spawn(*args, **kwargs)

    runtime.spawn = watching
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert seen == ["pending"]
    assert [row["state"] for row in store.launches("sw")] == ["started"]


class HiveAcks(FakeRuntime):
    def __init__(self, fail=()):
        super().__init__()
        self.ordered, self.collected, self.acked, self.failing, self.broken = [], [], set(), set(fail), set()
        self.refused = set()

    def spawn(self, config, lane, name, task):
        from scripts.swarm.tick import ACK_LATER

        if lane == MASTER:
            return super().spawn(config, lane, name, task)
        assert task[ACK_LATER] is True
        if task["id"] in self.refused:
            raise SpawnError("hive refused the order", "refused")
        self.ordered.append(name)
        raise SpawnError("hive launch ordered, waiting for its acknowledgement", "pending")

    def collect(self, config, lane, name, task):
        self.collected.append(name)
        if name in self.broken:
            raise ConnectionError("redis went away")
        if name in self.failing:
            raise SpawnError("hive launch failed: prompt", "refused", pane_id=f"pane-{name}")
        if name not in self.acked:
            raise SpawnError("hive launch ordered, waiting for its acknowledgement", "pending")
        return super().spawn(config, lane, name, task)


def test_a_tick_pass_never_waits_on_hive_acknowledgements_and_a_later_pass_records_them(store):
    store.update("sw", max_eng=3)
    ledger, runtime = tasks(("t1", "eng"), ("t2", "eng"), ("t3", "eng")), HiveAcks()
    actions = tick("sw", store, ledger, runtime, now_ms=1_000)
    names = list(runtime.ordered)
    waits = {f"{ledger.rows[t]['claimed_by']} waits for the hive acknowledgement of {t}" for t in ("t1", "t2", "t3")}
    assert waits <= set(actions)
    assert len(names) == 3 and runtime.spawned == []
    assert sorted(row["state"] for row in store.launches("sw")) == ["acknowledging"] * 3
    tick("sw", store, ledger, runtime, now_ms=STARTUP_GRACE_MS + 2_000)
    assert sorted(runtime.collected) == sorted(names) and runtime.ordered == names
    assert [store.claimant("sw", t) for t in ("t1", "t2", "t3")] == [
        ledger.rows[t]["claimed_by"] for t in ("t1", "t2", "t3")
    ]
    assert all(ledger.rows[t]["claimed_by"] in names for t in ("t1", "t2", "t3"))
    runtime.acked.update(names)
    tick("sw", store, ledger, runtime, now_ms=STARTUP_GRACE_MS + 3_000)
    assert sorted(name for _, name, _ in runtime.spawned) == sorted(names)
    assert {lane for lane, _, _ in runtime.spawned} == {"eng"}
    assert sorted(row["state"] for row in store.launches("sw")) == ["started"] * 3
    assert [store.claims("sw", t) for t in ("t1", "t2", "t3")] == [1, 1, 1]
    assert runtime.ordered == names


@pytest.mark.wall_clock
def test_a_tick_pass_with_three_unacknowledged_hive_orders_ends_within_seconds(store):
    import time

    store.update("sw", max_eng=3)
    ledger, runtime = tasks(("t1", "eng"), ("t2", "eng"), ("t3", "eng")), HiveAcks()
    started = time.monotonic()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert time.monotonic() - started < 5
    assert len(runtime.ordered) == 3


def test_a_refused_hive_acknowledgement_reopens_its_task(store):
    from scripts.swarm import failed_panes

    store.update("sw", max_eng=2)
    ledger, runtime = tasks(("t1", "eng"), ("t2", "eng")), HiveAcks()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    names = list(runtime.ordered)
    runtime.failing.update(names)
    actions = tick("sw", store, ledger, runtime, now_ms=2_000)
    assert sum(action.startswith("spawn failed for ") for action in actions) == 2
    launches = {row["agent"]: row for row in store.launches("sw")}
    for name in names:
        assert (launches[name]["state"], launches[name]["error"]) == ("failed", "hive launch failed: prompt")
        assert failed_panes.held(store, "sw")[f"pane-{name}"]["failed_at"] == 2_000
    assert not set(names) & {agent.name for agent in store.agents("sw")}
    assert ledger.rows["t1"]["claimed_by"] not in names and ledger.rows["t2"]["claimed_by"] not in names


def test_every_refused_hive_order_in_one_pass_reports_its_failure(store):
    store.update("sw", max_eng=2)
    ledger, runtime = tasks(("t1", "eng"), ("t2", "eng")), HiveAcks()
    runtime.refused.update({"t1", "t2"})
    actions = tick("sw", store, ledger, runtime, now_ms=1_000)
    failures = sorted(action for action in actions if action.startswith("spawn failed for "))
    assert len(failures) == 2
    assert [failure.startswith(f"spawn failed for {t}") for failure, t in zip(failures, ("t1", "t2"))] == [True, True]
    assert all(failure.endswith(": hive refused the order") for failure in failures)
    assert runtime.ordered == []


def test_a_transient_collect_error_keeps_the_claim_until_the_hive_acknowledges(store, monkeypatch):
    from scripts.swarm_v2.runtime import codex

    admitted, admit = [], codex.admit_legacy
    monkeypatch.setattr(codex, "admit_legacy", lambda *args: admitted.append(args[0]) or admit(*args))
    ledger, runtime = tasks(("t1", "eng")), HiveAcks()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    first = runtime.ordered[0]
    runtime.broken.add(first)
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert [row["state"] for row in store.launches("sw")] == ["acknowledging"]
    assert ledger.rows["t1"]["claimed_by"] == first and store.claimant("sw", "t1") == first
    runtime.broken.clear()
    runtime.acked.add(first)
    tick("sw", store, ledger, runtime, now_ms=3_000)
    assert [row["state"] for row in store.launches("sw")] == ["started"]
    assert runtime.ordered == [first]
    assert admitted and all(seen is store for seen in admitted)


def test_a_launch_no_hive_acknowledges_within_the_claim_lease_reopens_its_task(store):
    from scripts.swarm.tick import ACK_EXPIRED, LEASE_MS

    ledger, runtime = tasks(("t1", "eng")), HiveAcks()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    first = runtime.ordered[0]
    tick("sw", store, ledger, runtime, now_ms=1_000 + LEASE_MS - 1)
    assert [row["state"] for row in store.launches("sw")] == ["acknowledging"]
    collected = len(runtime.collected)
    tick("sw", store, ledger, runtime, now_ms=1_000 + LEASE_MS)
    launches = {row["agent"]: row for row in store.launches("sw")}
    assert (launches[first]["state"], launches[first]["error"]) == ("failed", ACK_EXPIRED)
    assert len(runtime.collected) == collected
    assert first not in {agent.name for agent in store.agents("sw")}
    assert ledger.rows["t1"]["claimed_by"] != first


def test_no_free_session_slot_claims_nothing(store):
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime(full=True)
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert ledger.rows["t1"]["state"] == "open" and store.claimant("sw", "t1") is None and runtime.spawned == []
    assert runtime.capacity_for == ["sw", "sw"]


class SessionsLeft(FakeRuntime):
    def __init__(self, sessions):
        super().__init__()
        self.sessions = sessions

    def has_capacity(self, config):
        super().has_capacity(config)
        return len(self.spawned) < self.sessions


def test_with_one_free_session_the_lane_without_a_live_agent_spawns_first(store):
    ledger, runtime = tasks(("t1", "eng")), SessionsLeft(1)
    tick("sw", store, ledger, runtime, now_ms=1_000)
    ledger.rows.update(tasks(("t2", "eng"), ("t3", "ci")).rows)
    runtime.sessions = 2
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert [task for _, _, task in runtime.spawned] == ["t1", "t3"]
    assert ledger.rows["t2"]["state"] == "open" and store.claimant("sw", "t2") is None


def test_every_lane_takes_a_session_before_any_lane_takes_a_second(store):
    ledger, runtime = tasks(("t1", "eng"), ("t2", "eng"), ("t3", "ci")), SessionsLeft(2)
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert runtime.spawned == [("eng", "engineer@a1b2c3-0001", "t1"), ("ci", "ci@a1b2c3-0001", "t3")]
    assert ledger.rows["t2"]["state"] == "open"


def test_the_spawn_pass_says_when_it_stops_at_the_session_cap(store):
    actions = tick("sw", store, tasks(("t1", "eng"), ("t2", "eng")), SessionsLeft(1), now_ms=1_000)
    assert "every agent is at its session cap, waiting" in actions


def _slow_launches(monkeypatch, runtime, seconds):
    from types import SimpleNamespace

    from scripts.swarm import tick as tick_module

    clock = [0.0]
    monkeypatch.setattr(tick_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    spawn = runtime.spawn

    def slow(config, lane, name, task):
        clock[0] += seconds
        return spawn(config, lane, name, task)

    runtime.spawn = slow


def test_a_launch_that_spends_the_budget_leaves_the_next_task_for_the_next_pass(store, monkeypatch):
    from scripts.swarm.tick import SPAWN_BUDGET_S

    assert SPAWN_BUDGET_S == 20
    ledger, runtime = tasks(("t1", "eng"), ("t2", "eng")), FakeRuntime()
    _slow_launches(monkeypatch, runtime, 20)
    actions = tick("sw", store, ledger, runtime, now_ms=1_000)
    assert runtime.spawned == [("eng", "engineer@a1b2c3-0001", "t1")]
    assert "spent this pass's 20 second launch budget, t2 waits a pass" in actions
    assert ledger.rows["t2"].get("claimed_by", "") == ""
    assert ledger.rows["t2"]["state"] == "open"
    assert not store.redis.exists(store.key("sw", "claim", "t2"))


def test_time_spent_before_the_first_launch_never_holds_it_back(store, monkeypatch):
    from types import SimpleNamespace

    from scripts.swarm import tick as tick_module

    clock = [0.0]
    monkeypatch.setattr(tick_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    stop = tick_module._spawn_stop

    def slow_stop(*args):
        clock[0] += 30.0
        return stop(*args)

    monkeypatch.setattr(tick_module, "_spawn_stop", slow_stop)
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert [task for _, _, task in runtime.spawned] == ["t1"]


def test_launches_inside_the_budget_keep_filling_the_lanes(store, monkeypatch):
    ledger, runtime = tasks(("t1", "eng"), ("t2", "eng")), FakeRuntime()
    _slow_launches(monkeypatch, runtime, 19.9)
    actions = tick("sw", store, ledger, runtime, now_ms=1_000)
    assert [task for _, _, task in runtime.spawned] == ["t1", "t2"]
    assert not any("launch budget" in action for action in actions)


def test_a_slow_failed_launch_spends_the_budget_too(store, monkeypatch):
    ledger, runtime = tasks(("t1", "eng"), ("t2", "eng")), FailingFor("t1")
    _slow_launches(monkeypatch, runtime, 30)
    actions = tick("sw", store, ledger, runtime, now_ms=1_000)
    assert runtime.tried == ["t1"]
    assert "spent this pass's 20 second launch budget, t2 waits a pass" in actions


def test_a_spawn_claims_its_task_for_one_lease_and_records_its_seat_at_launch_time(store):
    from scripts.swarm.tick import LEASE_MS

    ledger, runtime, seen = tasks(("t1", "eng")), FakeRuntime(), []
    spawn = runtime.spawn

    def watching(config, lane, name, task):
        seen.extend(a.state for a in store.agents("sw") if a.name == name and lane != MASTER)
        return spawn(config, lane, name, task)

    runtime.spawn = watching
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert seen == ["starting"]
    assert 0 < store.redis.pttl(store.key("sw", "claim", "t1")) <= LEASE_MS
    assert [e["at"] for e in store.seats.history("eng-1@sw")] == [1_000]
    assert "kind" not in ledger.rows["t1"]


def test_a_task_claimed_elsewhere_mid_pass_does_not_stop_the_tasks_behind_it(store):
    ledger, runtime = tasks(("t1", "eng"), ("t2", "eng")), FakeRuntime()
    claim = store.claim
    store.claim = lambda slug, task, agent, lease: task != "t1" and claim(slug, task, agent, lease)
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert [task for _, _, task in runtime.spawned] == ["t2"]


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
    store.update("sw", autonomy="assist")
    ledger, runtime = DoneMidTick([{"id": "t1", "lane": "eng"}, {"id": "t2", "lane": "eng"}]), FakeRuntime()
    for row in ledger.rows.values():
        row["difficulty"] = "M"
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


def test_a_master_block_during_a_tick_spawns_no_agent(store):
    ledger, runtime = DoneMidTick([{"id": "t1", "lane": "eng"}]), FakeRuntime()
    ledger.rows["t1"]["difficulty"] = "M"
    ledger.closing = lambda: ledger.rows["t1"].update(state="blocked", claimed_by="")
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert ledger.rows["t1"]["state"] == "blocked"
    assert store.claimant("sw", "t1") is None
    assert runtime.spawned == []
    assert workers(store) == []


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
    store.update("sw", autonomy="assist")
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
    nudges = len(stale_nudges(store))
    actions = tick("sw", store, ledger, runtime, now_ms=2_000 + IDLE_KILL_TICKS * 60_000)
    assert nudges == 1 and len(stale_nudges(store, "eng-1@sw") + stale_nudges(store)) == 1
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
    store.update("sw", autonomy="assist")
    ledger, runtime = DoneMidTick([{"id": "t1", "lane": "eng", "difficulty": "M"}]), FakeRuntime()
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


def test_a_task_blocked_on_dev_red_is_reopened_once_dev_tests_passes(store, monkeypatch):
    from scripts.swarm import dev_red

    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    ledger.comments = []
    ledger.comment = lambda slug, task, text, by: ledger.comments.append((task, text, by))
    store.update("sw", state="paused")
    ledger.rows["t1"].update(state="blocked", claimed_by="engineer@a1b2c3-0009")
    dev_red.hold(store.redis, "sw", "t1", 41)
    read = []
    monkeypatch.setattr(
        dev_red, "latest", lambda repo, run=None: read.append(repo) or {"id": 42, "conclusion": "success"}
    )
    actions = tick("sw", store, ledger, runtime, now_ms=1_000)
    assert read == ["/repo"]
    assert (ledger.rows["t1"]["state"], ledger.rows["t1"]["claimed_by"]) == ("open", "")
    assert ("t1", dev_red.REOPENED, "swarm") in ledger.comments
    assert "task t1 reopened, dev Tests passed after the red run that blocked it" in actions


def test_a_task_held_by_a_known_agent_is_not_reopened_as_an_orphan(store):
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    store.update("sw", state="paused")
    store.put_agent("sw", AgentRecord("engineer@a1b2c3-0009", "eng", "t1", started_at=1_000))
    runtime.live.add("engineer@a1b2c3-0009")
    ledger.rows["t1"].update(state="claimed", claimed_by="engineer@a1b2c3-0009")
    actions = tick("sw", store, ledger, runtime, now_ms=1_000)
    assert (ledger.rows["t1"]["state"], ledger.rows["t1"]["claimed_by"]) == ("claimed", "engineer@a1b2c3-0009")
    assert "task t1 had no agent, reopened" not in actions


def test_an_idle_agent_is_retired_without_a_typed_nudge_and_its_task_reopened(store):
    from scripts.swarm.tick import IDLE_KILL_TICKS

    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    runtime.statuses["engineer@a1b2c3-0001"] = "idle"
    store.update("sw", state="paused")
    for n in range(IDLE_KILL_TICKS):
        tick("sw", store, ledger, runtime, now_ms=2_000 + n)
    assert runtime.nudged == []
    assert runtime.killed == ["engineer@a1b2c3-0001"] and ledger.rows["t1"]["state"] == "open"


STALE_MS = 10 * 60_000
IDLER = "engineer@a1b2c3-0001"


def spawned(store, status="idle", runtime=None):
    ledger, runtime = tasks(("t1", "eng")), runtime or FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    store.update("sw", state="paused")
    runtime.statuses[IDLER] = status
    return ledger, runtime


def stale_nudges(store, name=IDLER):
    from scripts.inbox.store import InboxStore
    from scripts.swarm import stale_nudge

    return [item for item in InboxStore(store.redis).inbox(name) if stale_nudge.is_nudge(item)]


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
    assert workers(store)[0].idle_ticks == IDLE_NUDGE_TICKS - 1


def test_a_finished_agent_that_will_not_die_stays_registered(store):
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    first = workers(store)[0]
    store.put_agent("sw", AgentRecord(**{**first.__dict__, "state": "finished"}))
    runtime.stuck.add("engineer@a1b2c3-0001")
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert [a.name for a in workers(store)] == ["engineer@a1b2c3-0001"]


def test_a_finished_agent_frees_its_slot_on_the_same_tick(store):
    store.update("sw", max_eng=1)
    ledger, runtime = tasks(("t1", "eng"), ("t2", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    ledger.rows["t1"]["state"] = "done"
    actions = tick("sw", store, ledger, runtime, now_ms=2_000)
    assert actions.index("retired engineer@a1b2c3-0001") < actions.index("spawned engineer@a1b2c3-0002 for t2")
    assert [a.name for a in workers(store)] == ["engineer@a1b2c3-0002"]


def test_every_tick_sends_the_ledger_its_time_left_inputs(store):
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    sent = []
    ledger.time_left = lambda slug, slots, ci_minutes: sent.append((slug, slots, ci_minutes))
    tick("sw", store, ledger, runtime, now_ms=1_000)
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert sent == [("sw", None, None), ("sw", None, None)]


def test_the_tick_sends_quota_slots_and_counts_ledger_events_against_its_clock(store):
    ledger, runtime = FakeLedger([{"id": "t0", "state": "done", "done": True}, {"id": "t1"}]), FakeRuntime()
    ledger.log = [
        {"kind": "task claimed", "target": "tasks/t0", "at": 1_000, "by": "eng", "rev": 1},
        {"kind": "task pr", "target": "tasks/t0", "at": 61_000, "by": "eng", "rev": 2},
    ]
    ledger.comment = lambda slug, item, text, by: None
    lanes = ("eng", "ci", "plan")
    runtime.quota_capacity = lambda cfg, agents, now, demand, requirements: {
        "configured": {"eng": 2, "ci": 1, "plan": 1},
        "effective": dict.fromkeys(lanes, 0),
        "placeable": {"claude": 3, "codex": 0},
        "reason": "accounts have quota",
        "accounts": [],
        "allocation": {lane: {"claude": 0, "codex": 0} for lane in lanes},
        "placements": {lane: [] for lane in lanes},
    }
    sent = []
    ledger.time_left = lambda slug, slots, ci_minutes: sent.append((slug, slots, ci_minutes))
    tick("sw", store, ledger, runtime, now_ms=120_000)
    assert sent == [("sw", 3, None)]


def test_a_finished_agent_whose_retire_fails_holds_no_lane_slot(store):
    store.update("sw", max_eng=1)
    ledger, runtime = tasks(("t1", "eng"), ("t2", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    first = workers(store)[0]
    store.put_agent("sw", replace(first, state="finished"))
    runtime.stuck.add(first.name)
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert runtime.spawned[-1] == ("eng", "engineer@a1b2c3-0002", "t2")


def test_an_agent_whose_task_ended_holds_no_lane_slot(store):
    store.update("sw", max_eng=1)
    ledger, runtime = tasks(("t1", "eng"), ("t2", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    ledger.rows["t1"]["state"] = "blocked"
    runtime.stuck.add("engineer@a1b2c3-0001")
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert runtime.spawned[-1] == ("eng", "engineer@a1b2c3-0002", "t2")


def test_a_working_agent_still_holds_its_lane_slot(store):
    store.update("sw", max_eng=1)
    ledger, runtime = tasks(("t1", "eng"), ("t2", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert [s[2] for s in runtime.spawned] == ["t1"]


def test_the_reap_retires_with_the_task_scratch_homes(store, scratch):
    homes, (ledger, runtime) = scratch("t1"), (tasks(("t1", "eng")), FakeRuntime())
    tick("sw", store, ledger, runtime, now_ms=1_000)
    ledger.rows["t1"]["state"] = "done"
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert runtime.homes["engineer@a1b2c3-0001"] == homes


def test_a_retire_failing_three_ticks_is_a_health_finding(store):
    from scripts.swarm import retire_watch

    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    first = workers(store)[0]
    store.put_agent("sw", replace(first, state="finished"))
    runtime.stuck.add(first.name)
    runtime.refusals[first.name] = {"process": 4242, "refusal": "signal to 4242 refused: Operation not permitted"}
    tick("sw", store, ledger, runtime, now_ms=2_000)
    tick("sw", store, ledger, runtime, now_ms=3_000)
    assert retire_watch.findings(store, "sw") == []
    tick("sw", store, ledger, runtime, now_ms=4_000)
    assert [(r["since"], r["at"]) for r in retire_watch.rows(store, "sw")] == [(2_000, 4_000)]
    (found,) = retire_watch.findings(store, "sw")
    assert found.id == f"retire-failed/{first.name}"
    assert found.evidence == ("process 4242", "refusal: signal to 4242 refused: Operation not permitted")
    runtime.stuck.clear()
    tick("sw", store, ledger, runtime, now_ms=5_000)
    assert retire_watch.findings(store, "sw") == []


def test_a_resumed_duplicate_is_reaped_on_the_tick_that_retires_its_agent(store):
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    first = workers(store)[0]
    runtime.duplicates.add(first.name)
    runtime.live.add("ci@d4e5f6-0001")
    ledger.rows["t1"]["state"] = "done"
    actions = tick("sw", store, ledger, runtime, now_ms=2_000)
    assert f"reaped stray {first.name}" in actions
    assert runtime.reaped == [first.name] and first.name not in runtime.live


def test_the_stray_sweep_spares_every_name_the_swarm_still_records_or_never_issued(store):
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    runtime.live |= {"engineer@d4e5f6-0001", "engineer@a1b2c3-0099", "operator-shell", ""}
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert runtime.reaped == []


def test_a_stray_that_will_not_die_is_retried_next_tick(store):
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    first = workers(store)[0]
    runtime.duplicates.add(first.name)
    runtime.stuck.add(f"{first.name}")
    store.drop_agent("sw", first.name, at=1_500)
    actions = tick("sw", store, ledger, runtime, now_ms=2_000)
    assert f"could not reap stray {first.name}, retrying next tick" in actions


def test_a_lost_agent_is_retired_with_its_task_scratch_homes(store, scratch):
    homes = scratch("t1")
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    runtime.live.discard("engineer@a1b2c3-0001")
    tick("sw", store, ledger, runtime, now_ms=2_000 + STARTUP_GRACE_MS)
    assert runtime.homes["engineer@a1b2c3-0001"] == homes


def test_a_stalled_agent_is_retired_with_its_task_scratch_homes(store, scratch):
    from scripts.swarm.tick import IDLE_KILL_TICKS

    homes = scratch("t1")
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    runtime.statuses["engineer@a1b2c3-0001"] = "idle"
    idle_for(store, ledger, runtime, IDLE_KILL_TICKS, start=2_000)
    assert runtime.killed == ["engineer@a1b2c3-0001"]
    assert runtime.homes["engineer@a1b2c3-0001"] == homes


def test_a_mismatched_agent_is_retired_with_its_task_scratch_homes(store, scratch):
    homes = scratch("t1")
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    runtime.bindings = lambda agents: {a.name: {"process": False} for a in agents if a.lane != MASTER}
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert runtime.homes["engineer@a1b2c3-0001"] == homes


def test_a_stopping_master_is_retired_with_the_master_scratch_homes(store, scratch, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_MASTER_RETIRE_HANDOFF_MINUTES", "0")
    homes, runtime = scratch(MASTER), FakeRuntime()
    tick("sw", store, tasks(), runtime, now_ms=1_000)
    store.update("sw", state="stopping")
    tick("sw", store, tasks(), runtime, now_ms=2_000)
    assert runtime.homes["master@a1b2c3-0001"] == homes


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


RECLAIMED = {
    "remote_branch": "engineer-a1b2c3-0001",
    "remote_head": "4f2a9c0",
    "continue_from": "origin/engineer-a1b2c3-0001",
    "fresh_reason": "none",
}


@pytest.fixture
def reclaims(monkeypatch):
    seen = []

    def lookup(repo, lives, recorded):
        seen.append((repo, lives, recorded))
        return RECLAIMED

    monkeypatch.setattr("scripts.swarm.tick.reclaim", lookup)
    return seen


def test_a_task_reclaimed_after_its_agent_was_lost_is_given_that_agents_branch(store, reclaims):
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    ledger.rows["t1"]["branch"] = "feature-x"
    runtime.live.discard("engineer@a1b2c3-0001")
    tick("sw", store, ledger, runtime, now_ms=2_000 + STARTUP_GRACE_MS)
    assert runtime.spawned[-1] == ("eng", "engineer@a1b2c3-0002", "t1")
    assert reclaims == [("/repo", ["engineer@a1b2c3-0001"], "feature-x")]
    assert runtime.tasks[-1]["reclaim"] == RECLAIMED
    assert store.reclaims("sw") == {"engineer@a1b2c3-0002": RECLAIMED}


def test_a_reclaim_looks_at_every_earlier_life_newest_first(store, reclaims):
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    runtime.live.discard("engineer@a1b2c3-0001")
    tick("sw", store, ledger, runtime, now_ms=2_000 + STARTUP_GRACE_MS)
    runtime.live.discard("engineer@a1b2c3-0002")
    tick("sw", store, ledger, runtime, now_ms=4_000 + 2 * STARTUP_GRACE_MS)
    assert reclaims[-1] == ("/repo", ["engineer@a1b2c3-0002", "engineer@a1b2c3-0001"], "")


def test_a_first_life_has_no_reclaim_to_look_up(store, reclaims):
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert reclaims == [] and "reclaim" not in runtime.tasks[-1]
    assert store.reclaims("sw") == {}


def test_a_reopened_task_with_a_branch_and_no_earlier_life_is_given_that_branch(store, reclaims):
    ledger, runtime = FakeLedger([{"id": "t1", "branch": "feature-x"}]), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert reclaims == [("/repo", [], "feature-x")]
    assert runtime.tasks[-1]["reclaim"] == RECLAIMED


def test_a_handed_off_task_revalidates_its_handoff_continuation(store, reclaims):
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    store.put_handoff("sw", "t1", "seam 1 green", envelope={"continue_from": "origin/engineer-a1b2c3-0001"})
    store.put_agent("sw", replace(workers(store)[0], state="finished"))
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert runtime.tasks[-1]["handoff_envelope"] == RECLAIMED
    assert reclaims == [("/repo", ["engineer@a1b2c3-0001"], "engineer-a1b2c3-0001")]
    assert "reclaim" not in runtime.tasks[-1]


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
    runtime.live.discard("master@a1b2c3-0001")
    assert tick("sw", store, tasks(), runtime, 3) == [] and len(runtime.masters) == 1


def test_account_reads_and_spawns_hold_the_placement_lock_and_the_rest_does_not(store, monkeypatch):
    from scripts.inbox import exits
    from scripts.swarm import quota_notice
    from scripts.swarm import tick as tick_module

    held, runtime, spawn = [], FakeRuntime(), FakeRuntime.spawn
    monkeypatch.setattr(exits, "sweep", lambda *args: held.append(("sweep", tick_module.PLACING.locked())))
    monkeypatch.setattr(
        quota_notice, "refresh", lambda *args: held.append(("quota", tick_module.PLACING.locked())) or []
    )
    monkeypatch.setattr(
        runtime,
        "spawn",
        lambda config, lane, name, task: (
            held.append((lane, tick_module.PLACING.locked())) or spawn(runtime, config, lane, name, task)
        ),
    )
    tick("sw", store, tasks(("t1", "eng")), runtime, 1)
    assert held == [("sweep", False), ("quota", True), ("master", True), ("eng", True)]
    assert not tick_module.PLACING.locked()


def test_the_tick_leaves_the_ci_budget_and_flake_passes_to_the_refresh_job(store, monkeypatch):
    from scripts.ci_budget import defects, flakes
    from scripts.swarm import ci_speed

    seen = []
    monkeypatch.setattr(defects, "refresh", lambda *args: seen.append("defects") or ["updated"])
    monkeypatch.setattr(flakes, "refresh", lambda *args: seen.append("flakes") or ["filed"])
    monkeypatch.setattr(ci_speed, "refresh", lambda *args: seen.append("speed") or [])
    actions = tick("sw", store, tasks(("t1", "eng")), FakeRuntime(), 1)
    assert seen == []
    assert "updated" not in actions and "filed" not in actions


def test_tick_captures_panes_before_any_retirement(store, monkeypatch):
    from scripts.swarm import pane_evidence
    from scripts.swarm import tick as module

    seen = []
    rt = FakeRuntime()
    monkeypatch.setattr(pane_evidence, "sweep", lambda *args: seen.append(("capture", args)) or ["captured"])
    monkeypatch.setattr(module, "_recover_master", lambda *args: seen.append(("recover", args)) or ["recovered"])
    assert tick("sw", store, tasks(("t1", "eng")), rt, 123)[:2] == ["captured", "recovered"]
    assert seen[0] == ("capture", ("sw", store, rt, 123))
    assert seen[1][0] == "recover"


def test_an_operator_write_to_a_stopped_swarm_starts_its_master_and_no_engineers(store):
    from scripts.inbox.store import InboxStore

    store.update("sw", state="stopped")
    InboxStore(store.redis).send("operator", "master@sw", "operator replied on the ledger")
    runtime = FakeRuntime()
    actions = tick("sw", store, tasks(("t1", "eng"), ("t2", "ci")), runtime, 1)
    assert actions[0] == "the operator wrote on the ledger, paused to start the master"
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


@pytest.mark.parametrize("state", ["stopped", "paused", "running"])
def test_only_reopened_swarms_run_normal_tick_passes(store, monkeypatch, state):
    from scripts.swarm import tick as module

    store.update("sw", state=state)
    seen = []
    monkeypatch.setattr(module.resume, "track_outages", lambda *args: seen.append("outages"))
    monkeypatch.setattr(module, "_verify", lambda *args: seen.append("verify") or [])
    monkeypatch.setattr(module.pane_evidence, "sweep", lambda *args: seen.append("panes") or [])
    monkeypatch.setattr(store.execution_registry, "reconcile", lambda *args: seen.append("executions") or [])
    tick("sw", store, tasks(("t1", "eng")), FakeRuntime(), 1)
    assert seen == ([] if state == "stopped" else ["panes", "executions", "outages", "verify"])


@pytest.mark.parametrize("wake", [False, True])
def test_stopped_swarm_skips_outer_tick_work_but_consumes_control_commands(store, monkeypatch, wake):
    from scripts.inbox.store import InboxStore
    from scripts.swarm import cli, command_runner

    store.update("sw", state="stopped")
    if wake:
        InboxStore(store.redis).send("operator", "master@sw", "operator replied on the ledger")
    consumed = []
    monkeypatch.setattr(command_runner, "consume", lambda *args: consumed.append(args) or ["controls checked"])
    monkeypatch.setattr(cli.outage, "providers", lambda *args: pytest.fail("stopped swarm probed providers"))
    runtime = FakeRuntime()
    actions = cli.run_tick(store, "sw", tasks(("t1", "eng")), runtime)
    assert actions[:1] == ["controls checked"]
    assert consumed == [(store, "sw")]
    assert store.config("sw").state == ("paused" if wake else "stopped")
    assert len(runtime.masters) == int(wake)
    assert runtime.spawned == []


def test_stopped_retired_swarm_wakes_only_when_a_new_task_arrives(store):
    import json

    store.update("sw", state="stopped")
    store.redis.set(store.key("sw", "master-retired-tasks"), json.dumps(["t1"]))
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    assert tick("sw", store, ledger, runtime, 1) == []
    assert store.config("sw").state == "stopped"
    assert runtime.masters == []
    ledger.rows["t2"] = {"id": "t2", "lane": "eng", "state": "open"}
    tick("sw", store, ledger, runtime, 2)
    assert store.config("sw").state == "paused"
    assert len(runtime.masters) == 1
    assert runtime.spawned == []
    assert store.redis.get(store.key("sw", "master-retired-tasks")) is None


def test_closed_doctor_stays_stopped_and_withdraws_its_wake_message(store, monkeypatch):
    from scripts.inbox.store import InboxStore
    from scripts.swarm import tick as module

    store.update("sw", state="stopped", template=module.priming.TEMPLATE)
    ledger, runtime = tasks(), FakeRuntime()
    monkeypatch.setattr(ledger, "closed", lambda slug: True)
    inbox = InboxStore(store.redis)
    inbox.send("operator", "master@sw", "operator wrote after closing")
    assert tick("sw", store, ledger, runtime, 1) == []
    assert store.config("sw").state == "stopped"
    assert runtime.masters == []
    assert inbox.pending_items("master@sw") == []


def test_newly_open_phase_sends_its_planning_notice_on_the_same_tick(store, monkeypatch):
    from scripts.inbox.store import InboxStore
    from scripts.swarm import cli

    ledger = FakeLedger([{"id": "t1", "phase": "p1", "state": "done"}])
    ledger.phases = [
        {"id": "p1", "title": "First", "planning": "manual"},
        {"id": "p2", "title": "Next", "planning": "auto", "depends_on": ["p1"]},
    ]

    def finish(*args):
        ledger.phases[0]["done"] = True
        return ["finished first phase"]

    def add(slug, row, by):
        ledger.rows[row["task"]] = {**row, "id": row["task"], "state": "open"}

    ledger.add_task = add
    monkeypatch.setattr(cli.phases, "phase_pass", finish)
    inbox = InboxStore(store.redis)
    actions = cli._phase_steps(inbox, store, "sw", ledger.state("sw"), ledger)
    assert "queued plan-p2 for phase p2" in actions
    assert ledger.rows["plan-p2"]["phase"] == "p2"
    assert any("phase p2 Next needs a plan" in item.text for item in inbox.inbox("master@sw"))


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


def _other_swarm(store):
    store.create(SwarmConfig("doc", "/repo", max_eng=2, max_ci=1))
    ledger = tasks()
    ledger.notify = lambda slug, text: None
    return ledger


def test_a_swarm_waiting_on_its_master_gets_the_next_slot_before_any_other_swarm_spawns(store):
    doc = _other_swarm(store)
    assert "no session slot for the master, waiting" in tick("doc", store, doc, FakeRuntime(full=True), 1)

    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    actions = tick("sw", store, ledger, runtime, 2)

    assert runtime.spawned == []
    assert "holding spawns: swarm doc waits on a session slot for its master" in actions

    tick("doc", store, doc, FakeRuntime(), 3)
    tick("sw", store, ledger, runtime, 4)
    assert runtime.spawned == [("eng", "engineer@a1b2c3-0001", "t1")]


def test_a_stale_master_wait_stops_holding_other_swarms(store):
    tick("doc", store, _other_swarm(store), FakeRuntime(full=True), 1)

    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, 1 + 11 * 60_000)

    assert runtime.spawned == [("eng", "engineer@a1b2c3-0001", "t1")]


def test_a_master_wait_holds_until_exactly_ten_minutes(store):
    from scripts.swarm.tick import MASTER_WAIT_MS, MASTER_WAITING

    store.redis.hset(MASTER_WAITING, "doc", 1)
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()

    assert "holding spawns: swarm doc waits on a session slot for its master" in tick(
        "sw", store, ledger, runtime, MASTER_WAIT_MS
    )
    tick("sw", store, ledger, runtime, 1 + MASTER_WAIT_MS)
    assert runtime.spawned == [("eng", "engineer@a1b2c3-0001", "t1")]


def test_several_master_waits_name_one_swarm(store):
    from scripts.swarm.tick import MASTER_WAITING

    store.redis.hset(MASTER_WAITING, mapping={"zed": 1, "doc": 1})

    actions = tick("sw", store, tasks(("t1", "eng")), FakeRuntime(), 2)

    assert [a for a in actions if a.startswith("holding spawns")] == [
        "holding spawns: swarm doc waits on a session slot for its master"
    ]


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


def test_a_critical_chain_successor_is_claimed_the_tick_after_its_predecessor_merges_while_other_work_waits(store):
    store.update("sw", max_eng=1)
    ledger = FakeLedger(
        [{"id": "a"}, {"id": "b", "depends_on": ["a"]}, {"id": "c", "depends_on": ["b"]}, {"id": "x"}, {"id": "y"}]
    )
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert spawned_ids(runtime) == ["a"]
    ledger.rows["a"]["state"] = "done"
    ledger.rows["x"]["rank"] = "urgent"
    ledger.rows["y"]["branch"] = "engineer-a1b2c3-0009"
    ledger.stamps = {"tasks/a/state": {"by": "engineer@a1b2c3-0001", "at": 61_000}, "tasks/x/rank": {"by": "operator"}}
    actions = tick("sw", store, ledger, runtime, now_ms=121_000)
    assert spawned_ids(runtime) == ["a", "b"]
    assert [ledger.rows[t]["state"] for t in ("x", "y", "c")] == ["open", "open", "open"]
    assert "chain guard claimed task b 60 seconds after task a merged on the critical chain" in actions
    assert (
        "b",
        "The dispatcher's chain guard gave this task the next free eng seat 60 seconds after the task before it "
        "on the critical chain merged.",
        "dispatcher",
    ) in ledger.comments


def test_a_task_starts_stacked_on_claimed_and_in_pr_dependencies_that_carry_a_branch(store):
    store.update("sw", max_eng=3)
    ledger = FakeLedger([{"id": "t1"}, {"id": "t0"}, {"id": "t2", "depends_on": ["t1", "t0", "t9"]}, {"id": "t9"}])
    ledger.rows["t9"].update(state="done", done=True)
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert spawned_ids(runtime) == ["t1", "t0"]
    ledger.rows["t1"]["branch"] = "engineer-a1b2c3-0001"
    ledger.rows["t0"].update(state="pr", branch="engineer-a1b2c3-0002")
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert spawned_ids(runtime) == ["t1", "t0", "t2"]
    assert runtime.tasks[-1]["stack_base"] == [
        {"task": "t1", "branch": "engineer-a1b2c3-0001"},
        {"task": "t0", "branch": "engineer-a1b2c3-0002"},
    ]
    assert runtime.tasks[0]["stack_base"] == []


@pytest.mark.parametrize("dependency_state", ["claimed", "pr"])
def test_retained_pull_waits_for_dependency_merge_before_resuming(store, reclaims, dependency_state):
    ledger = FakeLedger(
        [
            {"id": "t1"},
            {"id": "t2", "depends_on": ["t1"], "branch": "reviewed-work", "pr_url": "https://github.com/o/r/pull/3"},
        ]
    )
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    ledger.rows["t1"].update(state=dependency_state, branch="dependency-work")

    tick("sw", store, ledger, runtime, now_ms=2_000)

    assert spawned_ids(runtime) == ["t1"]
    assert ledger.rows["t2"]["state"] == "open"
    assert ledger.rows["t2"]["claimed_by"] == ""
    assert reclaims == []
    assert store.claimant("sw", "t2") is None
    assert store.claims("sw", "t2") == 0

    ledger.rows["t1"].update(state="done", done=True)
    tick("sw", store, ledger, runtime, now_ms=3_000)

    assert spawned_ids(runtime) == ["t1", "t2"]
    assert ledger.rows["t2"]["state"] == "pr"
    assert ledger.rows["t2"]["pr_url"] == "https://github.com/o/r/pull/3"
    assert reclaims == [("/repo", [], "reviewed-work")]
    assert runtime.tasks[-1]["stack_base"] == []


def test_a_stacked_claim_carries_the_repository_only_of_a_dependency_branch_in_another_repository(store, monkeypatch):
    asked = []
    monkeypatch.setattr(
        stack,
        "shell",
        lambda argv: asked.append(argv) or subprocess.CompletedProcess(argv, 0, "git@github.com:O/home.git\n", ""),
    )
    store.update("sw", max_eng=3)
    ledger = FakeLedger([{"id": "t1"}, {"id": "t0"}, {"id": "t2", "depends_on": ["t1", "t0"]}])
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    ledger.rows["t1"].update(branch="engineer-a1b2c3-0001", branch_repo="https://github.com/o/other")
    ledger.rows["t0"].update(branch="engineer-a1b2c3-0002", branch_repo="https://github.com/o/home")
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert spawned_ids(runtime) == ["t1", "t0", "t2"]
    assert runtime.tasks[-1]["stack_base"] == [
        {"task": "t1", "branch": "engineer-a1b2c3-0001", "repo": "https://github.com/o/other"},
        {"task": "t0", "branch": "engineer-a1b2c3-0002"},
    ]
    assert ["git", "-C", "/repo", "remote", "get-url", "origin"] in asked


def test_a_stacked_claim_names_the_recorded_repository_when_its_own_origin_cannot_be_read(store, monkeypatch):
    monkeypatch.setattr(stack, "shell", lambda argv: subprocess.CompletedProcess(argv, 128, "", "no origin\n"))
    ledger = FakeLedger([{"id": "t1"}, {"id": "t2", "depends_on": ["t1"]}])
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    ledger.rows["t1"].update(branch="engineer-a1b2c3-0001", branch_repo="https://github.com/o/other")
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert runtime.tasks[-1]["stack_base"] == [
        {"task": "t1", "branch": "engineer-a1b2c3-0001", "repo": "https://github.com/o/other"}
    ]


def test_a_stacked_claim_names_the_recorded_repository_when_git_cannot_run(store, monkeypatch):
    def refuse(argv):
        raise SwarmError("git -C could not run: no git")

    monkeypatch.setattr(stack, "shell", refuse)
    ledger = FakeLedger([{"id": "t1"}, {"id": "t2", "depends_on": ["t1"]}])
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    ledger.rows["t1"].update(branch="engineer-a1b2c3-0001", branch_repo="XXXX")
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert spawned_ids(runtime) == ["t1", "t2"]
    assert runtime.tasks[-1]["stack_base"] == [{"task": "t1", "branch": "engineer-a1b2c3-0001", "repo": "XXXX"}]


@pytest.mark.parametrize(
    ("state", "branch"), [("open", "engineer-x"), ("blocked", "engineer-x"), ("claimed", ""), ("pr", "")]
)
def test_a_dependency_that_is_not_active_or_has_no_branch_still_holds_the_task(store, state, branch):
    ledger = FakeLedger([{"id": "t1"}, {"id": "t2", "depends_on": ["t1"]}])
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    ledger.rows["t1"].update(state=state, branch=branch, out_of_scope=True)
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert spawned_ids(runtime) == ["t1"] and ledger.rows["t2"]["state"] == "open"


@pytest.mark.parametrize("field", ["depends_on", "parked_on"])
def test_a_task_naming_a_task_missing_from_the_ledger_is_held(store, field):
    ledger, runtime = FakeLedger([{"id": "t2", field: ["gone"]}]), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert runtime.spawned == [] and ledger.rows["t2"]["state"] == "open"


def test_a_parked_task_waits_for_its_blocker_and_then_takes_a_finish_claim_with_its_handoff(store):
    ledger = FakeLedger([{"id": "t1"}, {"id": "t2", "depends_on": ["t1"], "parked_on": ["t1"]}])
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    ledger.rows["t1"]["branch"] = "engineer-a1b2c3-0001"
    store.put_handoff("sw", "t2", "parked on its branch until t1 merges")
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert spawned_ids(runtime) == ["t1"] and ledger.rows["t2"]["state"] == "open"
    ledger.rows["t1"].update(state="done", done=True)
    tick("sw", store, ledger, runtime, now_ms=3_000)
    assert spawned_ids(runtime) == ["t1", "t2"]
    assert runtime.tasks[-1]["handoff"] == "parked on its branch until t1 merges"
    assert runtime.tasks[-1]["stack_base"] == []


def test_a_parked_task_takes_the_free_slot_the_tick_after_its_blocker_is_done_ahead_of_fresh_work(store):
    store.update("sw", max_eng=1)
    ledger = FakeLedger(
        [
            {"id": "t1", "state": "pr", "branch": "engineer-a1b2c3-0001"},
            {"id": "fresh"},
            {"id": "t2", "depends_on": ["t1"], "parked_on": ["t1"], "branch": "engineer-a1b2c3-0002"},
        ]
    )
    runtime = FakeRuntime()
    ledger.rows["t1"].update(state="done", done=True)
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert spawned_ids(runtime) == ["t2"] and ledger.rows["fresh"]["state"] == "open"


def test_a_reopened_task_with_a_branch_is_claimed_before_fresh_work_even_when_its_territory_overlaps(store):
    store.update("sw", max_eng=1)
    ledger, runtime = FakeLedger([{"id": "t1", "territory": ["hooks"]}]), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    later = FakeLedger(
        [
            {"id": "fresh", "territory": ["docs"]},
            {"id": "reopened", "branch": "engineer-a1b2c3-0003", "territory": ["hooks/x.py"]},
        ]
    )
    ledger.rows.update(later.rows)
    store.update("sw", max_eng=2)
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert spawned_ids(runtime) == ["t1", "reopened"] and ledger.rows["fresh"]["state"] == "open"


def test_a_fresh_task_of_a_higher_rank_still_goes_ahead_of_a_resumed_one(store):
    store.update("sw", autonomy="assist")
    store.update("sw", max_eng=1)
    ledger = FakeLedger([{"id": "resumed", "branch": "engineer-a1b2c3-0004"}, {"id": "urgent", "rank": "urgent"}])
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert spawned_ids(runtime) == ["urgent"]


def test_a_resumed_task_whose_launch_failed_takes_only_a_slot_left_over(store):
    store.update("sw", max_eng=1)
    ledger = FakeLedger([{"id": "resumed", "branch": "engineer-a1b2c3-0005"}, {"id": "fresh"}])
    store.note_launch_failure("sw", "resumed", "canary timeout")
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert spawned_ids(runtime) == ["fresh"]


@pytest.mark.parametrize(
    ("parked_on", "blocker_done", "lives"), [(["t1"], False, 0), ([], False, 1), (["t1"], True, 1)]
)
def test_only_a_claim_that_ends_parked_on_an_unfinished_blocker_keeps_its_claim_life(
    store, parked_on, blocker_done, lives
):
    ledger = FakeLedger([{"id": "t1"}, {"id": "t2", "depends_on": ["t1"]}])
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    ledger.rows["t1"]["branch"] = "engineer-a1b2c3-0001"
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert spawned_ids(runtime) == ["t1", "t2"] and store.claims("sw", "t2") == 1
    agent = next(a for a in workers(store) if a.task == "t2")
    ledger.rows["t1"].update(state="done" if blocker_done else "pr", done=blocker_done)
    ledger.rows["t2"]["parked_on"] = parked_on
    store.put_handoff("sw", "t2", "stopped here", seat=agent.seat)
    store.put_agent("sw", replace(agent, state="finished"))
    store.update("sw", state="paused")
    tick("sw", store, ledger, runtime, now_ms=3_000)
    assert agent.name in runtime.killed and ledger.rows["t2"]["state"] == "open"
    assert store.claims("sw", "t2") == lives


def test_an_overlapping_task_is_claimed_once_the_non_overlapping_work_is_taken(store):
    ledger = FakeLedger(
        [
            {"id": "t1", "territory": ["hooks"]},
            {"id": "t2", "territory": ["hooks/x.py"]},
            {"id": "t3", "territory": ["docs"]},
        ]
    )
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert spawned_ids(runtime) == ["t1", "t3"]
    store.update("sw", max_eng=3)
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert spawned_ids(runtime) == ["t1", "t3", "t2"]
    assert runtime.tasks[2]["overlaps"] == [{"task": "t1", "claimant": runtime.spawned[0][1], "areas": ["hooks/x.py"]}]
    assert runtime.tasks[0]["overlaps"] == [] and runtime.tasks[1]["overlaps"] == []


def test_a_broad_territory_never_stops_a_claim(store):
    ledger = FakeLedger(
        [
            {"id": "t1", "territory": ["scripts"]},
            {"id": "t2", "territory": ["scripts/swarm/tick.py", "docs"]},
            {"id": "t3", "lane": "ci", "territory": ["./scripts/"]},
        ]
    )
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert spawned_ids(runtime) == ["t1", "t3", "t2"]
    first, second = runtime.spawned[0][1], runtime.spawned[1][1]
    assert runtime.tasks[1]["overlaps"] == [{"task": "t1", "claimant": first, "areas": ["scripts"]}]
    assert runtime.tasks[2]["overlaps"] == [
        {"task": "t1", "claimant": first, "areas": ["scripts/swarm/tick.py"]},
        {"task": "t3", "claimant": second, "areas": ["scripts/swarm/tick.py"]},
    ]


def test_an_overlap_names_the_live_owner_after_its_missing_claimant_is_repaired(store):
    ledger = FakeLedger([{"id": "t1", "territory": ["hooks"]}])
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    del ledger.rows["t1"]["claimed_by"]
    ledger.rows["t2"] = {"id": "t2", "lane": "eng", "state": "open", "out_of_scope": False, "territory": ["hooks"]}
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert spawned_ids(runtime) == ["t1", "t2"]
    assert runtime.tasks[1]["overlaps"] == [{"task": "t1", "claimant": runtime.spawned[0][1], "areas": ["hooks"]}]


@pytest.mark.parametrize("claimant", ["engineer@a1b2c3-0099", "eng-1@sw", ""])
def test_an_overlap_released_for_checks_has_no_coordination_recipient(store, claimant):
    from scripts.swarm import prompt, ready

    ledger = FakeLedger(
        [
            {"id": "t1", "state": "pr", "awaiting": "merge", "claimed_by": claimant, "territory": ["hooks"]},
            {"id": "t2", "title": "Build it", "territory": ["hooks/x.py"]},
        ]
    )
    ready.save(store, "sw", "t1", {"agent": claimant})
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert spawned_ids(runtime) == ["t2"]
    overlap = runtime.tasks[0]["overlaps"]
    assert overlap == [{"task": "t1", "claimant": "", "areas": ["hooks/x.py"], "checks": True}]
    text = prompt.build("sw", "/repo", "eng", runtime.spawned[0][1], runtime.tasks[0])
    assert "Task t1, waiting on checks with no agent; no coordination note needed, shares hooks/x.py." in text
    assert f"claimed by {claimant}" not in text


def test_a_reused_seat_does_not_make_its_new_agent_the_owner_of_an_overlap(store):
    from scripts.swarm import prompt, ready

    ledger = FakeLedger([{"id": "other", "territory": ["docs"]}])
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    agent = store.agents("sw")[0]
    ledger.rows.update(
        FakeLedger(
            [
                {"id": "t1", "state": "pr", "awaiting": "merge", "claimed_by": agent.seat, "territory": ["hooks"]},
                {"id": "t2", "title": "Build it", "territory": ["hooks/x.py"]},
            ]
        ).rows
    )
    ready.save(store, "sw", "t1", {"agent": "departed"})
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert spawned_ids(runtime) == ["other", "t2"]
    overlap = runtime.tasks[1]["overlaps"]
    assert overlap == [{"task": "t1", "claimant": "", "areas": ["hooks/x.py"], "checks": True}]
    text = prompt.build("sw", "/repo", "eng", runtime.spawned[1][1], runtime.tasks[1])
    assert "waiting on checks with no agent; no coordination note needed" in text
    assert agent.name not in text and agent.seat not in text


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
    assert spawned_ids(runtime) == ["t1", "t3", "t2"]


def test_an_urgent_ready_task_is_claimed_ahead_of_an_older_normal_one(store):
    store.update("sw", autonomy="assist")
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


def test_an_urgent_task_sharing_a_territory_an_active_claim_holds_is_still_claimed(store):
    ledger = FakeLedger([{"id": "t1", "territory": ["hooks"]}])
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    ledger.rows["t2"] = {**ledger.rows["t1"], "id": "t2", "state": "open", "claimed_by": "", "rank": "urgent"}
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert spawned_ids(runtime) == ["t1", "t2"] and ledger.rows["t1"]["state"] == "claimed"


def test_an_urgent_task_wins_a_shared_territory_over_an_earlier_normal_one(store):
    store.update("sw", autonomy="assist")
    ledger = FakeLedger([{"id": "t1", "territory": ["hooks"]}, {"id": "t2", "rank": "urgent", "territory": ["hooks"]}])
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert spawned_ids(runtime) == ["t2", "t1"]


def test_an_urgent_overlapping_task_is_claimed_ahead_of_lower_ranked_clear_work(store):
    store.update("sw", max_eng=1)
    ledger = FakeLedger([{"id": "t1", "territory": ["hooks"]}])
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    ledger.rows["t2"] = {**ledger.rows["t1"], "id": "t2", "state": "open", "claimed_by": "", "rank": "urgent"}
    ledger.rows["t3"] = {**ledger.rows["t2"], "id": "t3", "rank": "low", "territory": ["docs"]}
    ledger.stamps = {f"tasks/{t}/rank": {"by": "operator"} for t in ("t2", "t3")}
    store.update("sw", max_eng=2)
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert spawned_ids(runtime) == ["t1", "t2"]


def test_equal_ranks_keep_ledger_order_and_ranks_order_the_rest(store):
    store.update("sw", autonomy="assist")
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


def test_claims_follow_rank_then_the_small_fast_clear_task_then_critical_path_depth(store):
    store.update("sw", autonomy="assist")
    store.update("sw", max_eng=6)
    ledger = FakeLedger(
        [
            {"id": "shallow", "phase": "p1"},
            {"id": "deep", "title": "deep", "rank": "normal", "phase": "p2"},
            {"id": "w1", "depends_on": ["deep"], "rank": "low", "phase": "p2"},
            {"id": "w2", "depends_on": ["w1"], "rank": "low", "phase": "p2"},
            {"id": "small", "difficulty": "S", "phase": "p3"},
            {"id": "top", "rank": "high", "phase": "p9"},
        ]
    )
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert spawned_ids(runtime) == ["top", "small", "deep", "shallow"]


def test_a_rank_change_applies_on_the_next_tick(store):
    store.update("sw", autonomy="assist")
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
    rt.harness = "codex"
    store.update("sw", max_eng=3)
    tick("sw", store, FakeLedger([{"id": "t4"}]), rt, 2000)
    assert store.spawns("sw") == {"claude": 3, "codex": 1}


def _conversations(store):
    return {a.name: a.conversation_id for a in store.agents("sw")}


def test_a_spawned_agent_gets_the_conversation_id_herdr_reports_for_its_pane(store):
    rt = FakeRuntime()
    rt.conversation_ids = {"w1:p1": "5c90d80c", "w1:m1": "15e33356"}
    tick("sw", store, FakeLedger([{"id": "t1"}]), rt, 1000)
    assert _conversations(store) == {"engineer@a1b2c3-0001": "5c90d80c", "master@a1b2c3-0001": "15e33356"}


def test_each_tick_refreshes_the_conversation_id_and_preserves_known_ids(store):
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
        "engineer@a1b2c3-0002": "other",
        "master@a1b2c3-0001": "",
    }
    del rt.conversation_ids["w1:p1"]
    tick("sw", store, ledger, rt, 4000)
    assert _conversations(store)["engineer@a1b2c3-0001"] == "resumed"


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
    from scripts.swarm.stale_nudge import is_nudge
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
    seat = [(i.id, i.state) for i in inbox.inbox("eng-1@sw") if not is_nudge(i)]
    assert seat == [(open_item.id, "pending")] and len(stale_nudges(store, "eng-1@sw")) == 1
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
