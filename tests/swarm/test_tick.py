from dataclasses import replace

import pytest

from scripts.swarm.store import EVENTS_ENGINE, MASTER, AgentRecord, RedisStore, SwarmConfig, SwarmError
from scripts.swarm.tick import MASTER_DOWN, STARTUP_GRACE_MS, Placed, SpawnError, tick
from tests.helpers import store_seed
from tests.swarm.ledger_items import LedgerItems


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


class FakeLedger(LedgerItems):
    stamps: dict[str, dict[str, object]]

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

    def join(self, slug, name, role):
        return None

    def notice(self, slug, task_id, text):
        self.comments.append((task_id, text, "swarm"))


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


class RefusingRuntime(FakeRuntime):
    def placement_refusal(self, lane, task):
        return "no headless hive for eng lists the repo /repo" if task.get("id") == "t1" else ""


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


def test_scale_down_waits_for_the_task_to_finish(store):
    ledger, runtime = tasks(("t1", "eng"), ("t2", "eng"), ("t3", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    store.update("sw", max_eng=1)
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert runtime.killed == [] and len(workers(store)) == 2


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
    assert store_seed.take_restart(store, "sw", "t1") == ""


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
        self.refused, self.collected_slugs, self.conflicted = set(), set(), set()
        self.expired, self.expire_broken, self.kept = [], {}, set()

    def expire(self, config, lane, name, task):
        from scripts.swarm.tick import ACK_LATER

        assert (config.slug, task) == ("sw", {"id": task["id"], "seat": task["seat"], ACK_LATER: True})
        assert task["seat"].startswith(f"{lane}-")
        self.expired.append(name)
        if name in self.expire_broken:
            raise self.expire_broken[name]
        if name in self.kept:
            return False

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
        from scripts.swarm.tick import ACK_LATER

        assert task[ACK_LATER] is True
        assert task["seat"].startswith(f"{lane}-")
        self.collected.append(name)
        self.collected_slugs.add(config.slug)
        if name in self.broken:
            raise ConnectionError("redis went away")
        if name in self.conflicted:
            raise SwarmError("hive launch acknowledgement does not match the live execution")
        if name in self.failing:
            raise SpawnError("hive launch failed: prompt", "refused", pane_id=f"pane-{name}")
        if name not in self.acked:
            raise SpawnError("hive launch ordered, waiting for its acknowledgement", "pending")
        return super().spawn(config, lane, name, task)


class SessionsLeft(FakeRuntime):
    def __init__(self, sessions):
        super().__init__()
        self.sessions = sessions

    def has_capacity(self, config):
        super().has_capacity(config)
        return len(self.spawned) < self.sessions


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
    return clock


def test_a_second_slow_failed_launch_spends_the_budget(store, monkeypatch):
    store.update("sw", max_eng=3)
    ledger, runtime = tasks(("t1", "eng"), ("t2", "eng"), ("t3", "eng")), FailingFor("t1", "t2")
    _slow_launches(monkeypatch, runtime, 30)
    actions = tick("sw", store, ledger, runtime, now_ms=1_000)
    assert runtime.tried == ["t1", "t2"]
    assert "spent this pass's 20 second launch budget, t3 waits a pass" in actions


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


def test_a_reopened_task_keeps_its_pull_request_link(store):
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    ledger.rows["t1"].update(state="pr", pr_url="https://github.com/o/r/pull/3")
    runtime.live.clear()
    runtime.full = True
    tick("sw", store, ledger, runtime, now_ms=1_000 + STARTUP_GRACE_MS + 1)
    assert ledger.rows["t1"]["state"] == "open" and ledger.rows["t1"]["pr_url"].endswith("/3")


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


def test_a_claimed_task_without_an_agent_is_reopened(store):
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    store.update("sw", state="paused")
    ledger.rows["t1"].update(state="claimed", claimed_by="engineer@a1b2c3-0009")
    actions = tick("sw", store, ledger, runtime, now_ms=1_000)
    assert ledger.rows["t1"]["state"] == "open"
    assert ledger.rows["t1"]["claimed_by"] == ""
    assert "task t1 had no agent, reopened" in actions


@pytest.mark.feature("swarm-tick-events-orphans")
def test_a_claimed_task_without_an_agent_is_reopened_under_the_events_engine(store: RedisStore) -> None:
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    store.update("sw", state="paused", engine=EVENTS_ENGINE)
    ledger.rows["t1"].update(state="claimed", claimed_by="engineer@a1b2c3-0009")
    actions = tick("sw", store, ledger, runtime, now_ms=1_000)
    assert (ledger.rows["t1"]["state"], ledger.rows["t1"]["claimed_by"]) == ("open", "")
    assert "task t1 had no agent, reopened" in actions


def _claimed_by_the_hub_before_the_orphan_write(
    ledger: FakeLedger, monkeypatch: pytest.MonkeyPatch, **taken: object
) -> None:
    update = ledger.update_task

    def hub_claims(
        slug: str,
        task_id: str,
        fields: dict[str, object],
        by: str = "swarm",
        if_state: tuple[str, ...] = (),
        if_match: dict[str, object] | None = None,
    ) -> dict[str, object]:
        ledger.rows[task_id].update(taken)
        return update(slug, task_id, fields, by, if_state, if_match)

    monkeypatch.setattr(ledger, "update_task", hub_claims)


@pytest.mark.feature("swarm-tick-events-orphans")
def test_an_orphan_reopen_keeps_a_claim_a_new_holder_took_after_the_pass_read(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    store.update("sw", state="paused", engine=EVENTS_ENGINE)
    ledger.rows["t1"].update(state="claimed", claimed_by="engineer@a1b2c3-0009", claim_generation=3)
    _claimed_by_the_hub_before_the_orphan_write(ledger, monkeypatch, claimed_by="engineer@a1b2c3-0010")
    actions = tick("sw", store, ledger, runtime, now_ms=1_000)
    assert (ledger.rows["t1"]["state"], ledger.rows["t1"]["claimed_by"]) == ("claimed", "engineer@a1b2c3-0010")
    assert "task t1 had no agent, reopened" not in actions


@pytest.mark.feature("swarm-tick-events-orphans")
def test_an_orphan_reopen_keeps_a_claim_whose_generation_moved_after_the_pass_read(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    store.update("sw", state="paused", engine=EVENTS_ENGINE)
    ledger.rows["t1"].update(state="claimed", claimed_by="engineer@a1b2c3-0009", claim_generation=3)
    _claimed_by_the_hub_before_the_orphan_write(ledger, monkeypatch, claim_generation=4)
    actions = tick("sw", store, ledger, runtime, now_ms=1_000)
    assert (ledger.rows["t1"]["state"], ledger.rows["t1"]["claim_generation"]) == ("claimed", 4)
    assert "task t1 had no agent, reopened" not in actions


@pytest.mark.feature("swarm-tick-events-orphans")
def test_a_claimed_task_with_no_claimant_field_or_generation_is_reopened(store: RedisStore) -> None:
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    store.update("sw", state="paused", engine=EVENTS_ENGINE)
    ledger.rows["t1"]["state"] = "claimed"
    del ledger.rows["t1"]["claimed_by"]
    actions = tick("sw", store, ledger, runtime, now_ms=1_000)
    assert (ledger.rows["t1"]["state"], ledger.rows["t1"]["claimed_by"]) == ("open", "")
    assert "task t1 had no agent, reopened" in actions


@pytest.mark.feature("swarm-tick-events-orphans")
def test_an_orphan_on_a_later_claim_generation_is_reopened(store: RedisStore) -> None:
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    store.update("sw", state="paused", engine=EVENTS_ENGINE)
    ledger.rows["t1"].update(state="claimed", claimed_by="engineer@a1b2c3-0009", claim_generation=3)
    actions = tick("sw", store, ledger, runtime, now_ms=1_000)
    assert (ledger.rows["t1"]["state"], ledger.rows["t1"]["claimed_by"]) == ("open", "")
    assert "task t1 had no agent, reopened" in actions


@pytest.mark.feature("swarm-tick-events-orphans")
def test_an_events_claim_held_by_a_hive_agent_record_stays_claimed(store: RedisStore) -> None:
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    store.update("sw", state="paused", engine=EVENTS_ENGINE)
    name = store.next_name("sw", "eng")
    store.start_execution("sw", AgentRecord(name, "eng", "t1", seat="eng-1@sw", started_at=1_000, hive="hive-2"))
    ledger.rows["t1"].update(state="claimed", claimed_by=name)
    actions = tick("sw", store, ledger, runtime, now_ms=1_000)
    assert (ledger.rows["t1"]["state"], ledger.rows["t1"]["claimed_by"]) == ("claimed", name)
    assert "task t1 had no agent, reopened" not in actions


@pytest.mark.feature("swarm-tick-events-orphans")
def test_an_events_claim_the_hub_holds_before_its_agent_record_stays_claimed(store: RedisStore) -> None:
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    store.update("sw", state="paused", engine=EVENTS_ENGINE)
    assert store.claim("sw", "t1", "engineer@a1b2c3-0009", 600_000)
    ledger.rows["t1"].update(state="claimed", claimed_by="engineer@a1b2c3-0009")
    actions = tick("sw", store, ledger, runtime, now_ms=1_000)
    assert (ledger.rows["t1"]["state"], ledger.rows["t1"]["claimed_by"]) == ("claimed", "engineer@a1b2c3-0009")
    assert "task t1 had no agent, reopened" not in actions


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
    tick("sw", store, ledger, runtime, now_ms=62_000)
    assert retire_watch.findings(store, "sw") == []
    tick("sw", store, ledger, runtime, now_ms=122_000)
    assert [(r["since"], r["at"]) for r in retire_watch.rows(store, "sw")] == [(2_000, 122_000)]
    (found,) = retire_watch.findings(store, "sw")
    assert found.id == f"retire-failed/{first.name}"
    assert found.evidence == ("process 4242", "refusal: signal to 4242 refused: Operation not permitted")
    runtime.stuck.clear()
    tick("sw", store, ledger, runtime, now_ms=182_000)
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


RECLAIMED = {
    "remote_branch": "engineer-a1b2c3-0001",
    "remote_head": "4f2a9c0",
    "continue_from": "origin/engineer-a1b2c3-0001",
    "fresh_reason": "none",
}


@pytest.fixture
def reclaims(monkeypatch):
    seen = []

    def lookup(repo, lives, recorded, heads=None):
        seen.append((repo, lives, recorded))
        return RECLAIMED

    monkeypatch.setattr("scripts.swarm.tick.reclaim", lookup)
    return seen


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
    from scripts.swarm import phases
    from scripts.swarm.tick import run_group
    from scripts.tick.steps import PassBox

    ledger = FakeLedger([{"id": "t1", "phase": "p1", "state": "done"}])
    ledger.phases = [
        {"id": "p1", "title": "First", "planning": "manual"},
        {"id": "p2", "title": "Next", "planning": "auto", "depends_on": ["p1"]},
    ]
    ledger.add_task = lambda slug, row, by: ledger.rows.update(
        {row["task"]: {**row, "id": row["task"], "state": "open"}}
    )
    monkeypatch.setattr(
        ledger, "set_phase", lambda slug, phase_id, done, status: ledger.phases[0].update(done=done), raising=False
    )
    monkeypatch.setattr(phases, "phase_changes", lambda *args: [phases.PhaseChange("p1", "First", True, 1, ())])
    inbox = InboxStore(store.redis)
    passing = {"store": store, "slug": "sw", "runtime": None, "box": PassBox(), "ledger": ledger}
    actions = run_group("wrapper phases", **passing, inbox=inbox, doc=ledger.state("sw"))
    assert "phase p1 ticked" in actions and "queued plan-p2 for phase p2" in actions
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


@pytest.mark.feature("swarm-resumed-agent-grace")
def test_a_master_resumed_inside_the_start_grace_posts_no_down_notice(store: RedisStore) -> None:
    from scripts.inbox.store import InboxStore
    from scripts.tick.steps import masters as master_steps

    start = 10**9
    runtime, ledger = FakeRuntime(), tasks()
    tick("sw", store, ledger, runtime, start)
    [boss] = masters(store)
    resumed_at = start + 2 * STARTUP_GRACE_MS
    store.put_agent("sw", replace(boss, launched_at=resumed_at))
    runtime.live.clear()
    InboxStore(store.redis).send("operator", "master@sw", "how far along")

    actions = master_steps._master_down("sw", store.config("sw"), store, ledger, runtime, resumed_at + STARTUP_GRACE_MS)

    assert actions == []
    assert MASTER_DOWN not in ledger.notes


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


def test_a_master_handoff_keeps_the_master_seat_and_bumps_its_generation(store):
    runtime = FakeRuntime()
    tick("sw", store, tasks(), runtime, 1)
    (old,) = masters(store)
    store.put_handoff("sw", MASTER, "doc", seat=old.seat)
    store.put_agent("sw", replace(old, state="finished"))
    tick("sw", store, tasks(), runtime, 2)
    assert store.seats.occupant("master@sw").occupant == "master@a1b2c3-0002"
    assert [e["generation"] for e in store.seats.history("master@sw")] == [1, 2]


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
