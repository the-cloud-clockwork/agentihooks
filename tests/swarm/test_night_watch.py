import json

import pytest

from scripts.gates import catalog
from scripts.gates import log as gate_log
from scripts.swarm import capacity, tick
from scripts.swarm.health import merge_stall, night_watch
from scripts.swarm.store import MANUAL_SCALING, AgentRecord, RedisStore, SwarmConfig

pytestmark = pytest.mark.unit

SLUG = "sw"
NOW = 1_800_000_000_000
MINUTE = 60_000
QUIET = {"repo": "o/r", "queue": 0, "merged": 0, "failed": "none observed", "streak": 0, "head": 0}


class Ledger:
    def __init__(self):
        self.alerts, self.closed = [], []

    def raise_alert(self, slug, source, text):
        self.alerts.append((source, text))

    def close_alert(self, slug, alert_id, outcome):
        self.closed.append((alert_id, outcome))


class Runtime:
    def __init__(self):
        self.live = set()

    def live_names(self):
        return set(self.live)


@pytest.fixture
def store(monkeypatch, tmp_path):
    import fakeredis

    saved = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    saved.create(SwarmConfig(SLUG, "/repo", max_eng=1, max_ci=0, max_plan=0, scaling=MANUAL_SCALING))
    saved.update(SLUG, state="running")
    monkeypatch.setattr(gate_log, "swarm_home", lambda: tmp_path)
    monkeypatch.setattr(merge_stall, "observe", lambda repo, at: {**QUIET, "at": at})
    monkeypatch.setattr(capacity, "ready_work", lambda slug, store, doc: ({}, {"eng": [], "ci": [], "plan": []}))
    return saved


def enforce(store):
    store.update(SLUG, gates={"night-watch": "enforce"})


class Rig:
    def __init__(self):
        self.ledger, self.runtime, self.respawned, self.mailed = Ledger(), Runtime(), [], []


def run(store, rig, at, doc=None, sleeping=False):
    def respawn():
        rig.respawned.append(at)
        return ["respawned the master"]

    def mail(subject, text, environ, repo):
        rig.mailed.append((subject, text, repo))
        return ""

    found = night_watch.Watch(
        store, SLUG, rig.ledger, rig.runtime, doc or {"tasks": []}, at, sleeping, respawn, environ={}, mail=mail
    )
    return night_watch.run(found)


def kinds(store):
    return [finding.kind for finding in night_watch.findings(store, SLUG)]


def test_the_night_watch_gate_defaults_to_log_only():
    assert catalog.defaults()["night-watch"] == "observe"


def test_an_empty_front_door_seat_logs_a_respawn_at_ten_minutes_in_log_only_mode(store, tmp_path):
    rig = Rig()
    assert run(store, rig, NOW) == []
    assert run(store, rig, NOW + 10 * MINUTE - 1) == []
    assert kinds(store) == []
    actions = run(store, rig, NOW + 10 * MINUTE)
    assert rig.respawned == [] and rig.ledger.alerts == []
    assert any(line.startswith("night watch, log only: would respawn the front door master") for line in actions)
    assert kinds(store) == ["empty master seat"]
    (finding,) = night_watch.findings(store, SLUG)
    assert finding.summary == "The front door master seat has been empty for 10 minutes"
    rows = gate_log.recent(SLUG, None, tmp_path)
    assert [(row.gate, row.kind, row.tool) for row in rows] == [
        ("night-watch", "observe", "respawn"),
        ("night-watch", "observe", "alert seat"),
    ]


def test_an_enforced_empty_seat_respawns_once_per_ten_minutes_and_alerts_once(store):
    enforce(store)
    rig = Rig()
    run(store, rig, NOW)
    run(store, rig, NOW + 10 * MINUTE)
    run(store, rig, NOW + 15 * MINUTE)
    run(store, rig, NOW + 20 * MINUTE)
    assert rig.respawned == [NOW + 10 * MINUTE, NOW + 20 * MINUTE]
    ((source, text),) = rig.ledger.alerts
    assert source == "seat"
    assert text.startswith("The front door master seat of swarm sw has been empty for 10 minutes: ")


def test_a_live_front_door_master_closes_the_seat_alert(store):
    enforce(store)
    rig = Rig()
    run(store, rig, NOW)
    run(store, rig, NOW + 10 * MINUTE)
    boss = AgentRecord("boss", "master", "master", state="working", seat="master@sw")
    store.put_agent(SLUG, boss)
    rig.runtime.live.add("boss")
    doc = {"tasks": [], "alerts": [{"id": "a1", "source": "seat", "state": "open"}]}
    assert run(store, rig, NOW + 11 * MINUTE, doc) == [
        "closed the seat alert: the front door master seat is held again"
    ]
    assert rig.ledger.closed == [("a1", "The night watch saw it clear: the front door master seat is held again")]
    assert kinds(store) == []


@pytest.mark.parametrize("state, sleeping", [("paused", False), ("stopped", False), ("running", True)])
def test_a_seat_left_empty_on_purpose_is_never_judged(store, state, sleeping):
    store.update(SLUG, state=state)
    rig = Rig()
    run(store, rig, NOW, sleeping=sleeping)
    run(store, rig, NOW + 60 * MINUTE, sleeping=sleeping)
    assert rig.respawned == [] and kinds(store) == []


def held_master(store, runtime):
    boss = AgentRecord("boss", "master", "master", state="working", seat="master@sw")
    store.put_agent(SLUG, boss)
    runtime.live.add("boss")


def ready(monkeypatch, tasks=1):
    rows = [{"id": f"t{n}", "lane": "eng"} for n in range(tasks)]
    monkeypatch.setattr(capacity, "ready_work", lambda slug, store, doc: ({}, {"eng": rows, "ci": [], "plan": []}))


def decide(store, **decision):
    store.redis.set(store.key(SLUG, "quota-capacity"), json.dumps(decision))


def test_ready_work_held_by_host_room_raises_a_finding_naming_it_at_thirty_minutes(store, monkeypatch):
    enforce(store)
    rig = Rig()
    held_master(store, rig.runtime)
    ready(monkeypatch, 2)
    decide(store, host={"room": 0, "granted_at": NOW, "limit": 4, "reason": "load is high"})
    run(store, rig, NOW)
    assert run(store, rig, NOW + 30 * MINUTE - 1) == []
    actions = run(store, rig, NOW + 30 * MINUTE)
    ((source, text),) = rig.ledger.alerts
    assert source == "spawn"
    assert text.startswith("Swarm sw has had 2 ready tasks and no agent launch for 30 minutes, held by host room: ")
    assert "load is high" in text
    assert actions == [f"raised the spawn alert: {text}"]
    (finding,) = night_watch.findings(store, SLUG)
    assert (finding.kind, finding.evidence[0]) == ("held spawn", "Hold: host room")


def test_ready_work_held_at_the_lane_cap_names_the_cap(store, monkeypatch):
    rig = Rig()
    held_master(store, rig.runtime)
    store.put_agent(SLUG, AgentRecord("e1", "eng", "engineer", state="working", started_at=NOW - MINUTE))
    ready(monkeypatch)
    decide(store, configured={"eng": 1, "ci": 0, "plan": 0}, placements={}, reason="accounts have quota")
    run(store, rig, NOW)
    run(store, rig, NOW + 30 * MINUTE)
    (finding,) = night_watch.findings(store, SLUG)
    assert finding.evidence == ("Hold: cap", "Evidence: eng 1 of 1 seats busy")
    assert rig.ledger.alerts == []


def test_ready_work_with_free_seats_and_no_placement_names_quota(store, monkeypatch):
    rig = Rig()
    held_master(store, rig.runtime)
    ready(monkeypatch)
    decide(store, configured={"eng": 1, "ci": 0, "plan": 0}, placements={"eng": []}, reason="accounts are drain")
    run(store, rig, NOW)
    run(store, rig, NOW + 30 * MINUTE)
    (finding,) = night_watch.findings(store, SLUG)
    assert finding.evidence == ("Hold: quota", "Evidence: accounts are drain")


def test_a_launch_restarts_the_held_spawn_clock(store, monkeypatch):
    rig = Rig()
    held_master(store, rig.runtime)
    ready(monkeypatch)
    run(store, rig, NOW)
    store.put_agent(SLUG, AgentRecord("e1", "eng", "engineer", state="working", started_at=NOW + 20 * MINUTE))
    run(store, rig, NOW + 20 * MINUTE)
    run(store, rig, NOW + 30 * MINUTE)
    assert kinds(store) == []


def stalled(monkeypatch, **fields):
    observed = {**QUIET, "queue": 3, "merged": NOW - 61 * MINUTE, **fields}
    monkeypatch.setattr(merge_stall, "observe", lambda repo, at: {**observed, "at": at})


def test_an_enforced_merge_stall_alerts_and_emails_the_operator_once(store, monkeypatch):
    enforce(store)
    rig = Rig()
    held_master(store, rig.runtime)
    stalled(monkeypatch)
    run(store, rig, NOW)
    run(store, rig, NOW + 6 * MINUTE)
    ((source, text),) = rig.ledger.alerts
    assert source == "merge"
    assert text.startswith("Swarm sw dev merges are stuck: Dev has had no merge for 61 minutes with 3 queued entries")
    ((subject, body, repo),) = rig.mailed
    assert (subject, body, repo) == ("Swarm sw: dev merges are stuck", text, "/repo")


def test_three_failing_merge_groups_in_a_row_alert_without_an_hour_stall(store, monkeypatch):
    enforce(store)
    rig = Rig()
    held_master(store, rig.runtime)
    stalled(monkeypatch, merged=NOW - 5 * MINUTE, streak=3, head=99)
    run(store, rig, NOW)
    ((_, text),) = rig.ledger.alerts
    assert "The last 3 merge group runs failed in a row with 3 queued entries" in text
    assert "Newest failed merge group run: 99" in text
    assert len(rig.mailed) == 1


def test_a_logged_merge_stall_sends_no_email(store, monkeypatch, tmp_path):
    rig = Rig()
    held_master(store, rig.runtime)
    stalled(monkeypatch)
    actions = run(store, rig, NOW)
    assert rig.ledger.alerts == [] and rig.mailed == []
    assert actions[0].startswith("night watch, log only: would raise the merge alert: ")
    assert [row.tool for row in gate_log.recent(SLUG, None, tmp_path)] == ["alert merge"]


def test_the_failing_streak_counts_newest_failures_past_cancelled_runs(monkeypatch):
    runs = [
        {"id": 5, "conclusion": "cancelled"},
        {"id": 4, "conclusion": "failure"},
        {"id": 3, "conclusion": "timed_out"},
        {"id": 2, "conclusion": "success"},
        {"id": 1, "conclusion": "failure"},
    ]
    monkeypatch.setattr(merge_stall, "request", lambda repo, args: {"workflow_runs": runs})
    assert merge_stall.failing("/repo", "o/r") == (2, 4)


def test_the_tick_respawns_the_front_door_past_the_host_hold_and_spent_retries(store, monkeypatch):
    called = []
    store.redis.set(store.key(SLUG, "master-start"), json.dumps({"alerted": True}))
    monkeypatch.setattr(tick, "_master", lambda *args, **kwargs: called.append(kwargs) or ["launched"])
    seen = {}

    def run_watch(found):
        seen["respawn"] = found.respawn()
        return []

    monkeypatch.setattr(night_watch, "run", run_watch)
    tick._night_watch(SLUG, store, Ledger(), Runtime(), {"tasks": []}, NOW, False)
    assert seen["respawn"] == ["launched"]
    assert called == [{"past_host": True}]
    assert not store.redis.exists(store.key(SLUG, "master-start"))
