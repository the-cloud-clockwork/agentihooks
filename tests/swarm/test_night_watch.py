import json
import subprocess

import pytest

from scripts.gates import catalog
from scripts.gates import log as gate_log
from scripts.swarm import capacity, operator_email, status, tick
from scripts.swarm.health import merge_stall, night_watch
from scripts.swarm.health.findings import Finding
from scripts.swarm.store import MANUAL_SCALING, AgentRecord, RedisStore, SwarmConfig
from tests.swarm.test_cli import env as env

pytestmark = pytest.mark.unit

SLUG = "sw"
NOW = 1_800_000_000_000
MINUTE = 60_000
STEP = 2 * MINUTE
QUIET = {"repo": "o/r", "queue": 0, "merged": 0, "failed": "none observed", "streak": 0, "head": 0}


class Ledger:
    def __init__(self):
        self.alerts, self.closed, self.slugs, self.closed_slugs = [], [], [], []

    def raise_alert(self, slug, source, text):
        self.slugs.append(slug)
        self.alerts.append((source, text))

    def close_alert(self, slug, alert_id, outcome):
        self.closed_slugs.append(slug)
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
    store.update(SLUG, gates={"nightwatch": "enforce"})


class Rig:
    def __init__(self, refused="", respawns=("respawned the master",)):
        self.ledger, self.runtime, self.respawned, self.mailed, self.envs = Ledger(), Runtime(), [], [], []
        self.refused, self.respawns = refused, list(respawns)


def run(store, rig, at, doc=None, sleeping=False, environ=None):
    def respawn():
        rig.respawned.append(at)
        return list(rig.respawns)

    def mail(subject, text, env, repo):
        rig.mailed.append((subject, text, repo))
        rig.envs.append(env)
        return rig.refused

    found = night_watch.Watch(
        store, SLUG, rig.ledger, rig.runtime, doc or {"tasks": []}, at, sleeping, respawn, environ or {}, mail
    )
    return night_watch.run(found)


def until(store, rig, start, stop, **extra):
    actions, at = [], start
    while at < stop:
        actions += run(store, rig, at, **extra)
        at += STEP
    return actions + run(store, rig, stop, **extra)


def kinds(store):
    return [finding.kind for finding in night_watch.findings(store, SLUG)]


def test_the_night_watch_gate_defaults_to_log_only():
    assert catalog.defaults()["nightwatch"] == "observe"


def test_an_empty_front_door_seat_logs_a_respawn_at_ten_minutes_in_log_only_mode(store, tmp_path):
    rig = Rig()
    assert until(store, rig, NOW, NOW + 10 * MINUTE - 1) == []
    assert kinds(store) == []
    actions = run(store, rig, NOW + 10 * MINUTE)
    assert rig.respawned == [] and rig.ledger.alerts == []
    assert any(line.startswith("night watch gate not at deny: would respawn the front door master") for line in actions)
    assert kinds(store) == ["empty master seat"]
    (finding,) = night_watch.findings(store, SLUG)
    assert finding.summary == "The front door master seat has been empty for 10 minutes"
    rows = gate_log.recent(SLUG, None, tmp_path)
    assert [(row["gate"], row["kind"], row["tool"], row["agent"], row["at"]) for row in rows] == [
        ("nightwatch", "observe", "respawn", "swarm", NOW + 10 * MINUTE),
        ("nightwatch", "observe", "alert seat", "swarm", NOW + 10 * MINUTE),
    ]
    assert f"night watch gate not at deny: would raise the seat alert: {rows[1]['reason']}" in actions
    assert run(store, rig, NOW + 11 * MINUTE) == []


def test_an_enforced_empty_seat_respawns_once_per_ten_minutes_and_alerts_once(store):
    enforce(store)
    rig = Rig()
    until(store, rig, NOW, NOW + 20 * MINUTE)
    assert rig.respawned == [NOW + 10 * MINUTE, NOW + 20 * MINUTE]
    ((source, text),) = rig.ledger.alerts
    assert source == "seat"
    assert text.startswith("The front door master seat of swarm sw has been empty for 10 minutes: ")


def test_the_watch_respawns_an_empty_seat_at_most_three_times_an_hour(store):
    enforce(store)
    rig = Rig()
    until(store, rig, NOW, NOW + 60 * MINUTE)
    assert rig.respawned == [NOW + 10 * MINUTE, NOW + 20 * MINUTE, NOW + 30 * MINUTE]
    assert len(rig.ledger.alerts) == 1
    until(store, rig, NOW + 62 * MINUTE, NOW + 70 * MINUTE)
    assert rig.respawned[-1] == NOW + 70 * MINUTE


def test_the_log_counts_would_be_respawns_against_the_same_cap(store, tmp_path):
    rig = Rig()
    until(store, rig, NOW, NOW + 60 * MINUTE)
    assert [row["tool"] for row in gate_log.recent(SLUG, None, tmp_path)].count("respawn") == 3


def test_a_skipped_gate_logs_and_reports_nothing(store, tmp_path):
    store.update(SLUG, gates={"nightwatch": "off"})
    rig = Rig()
    assert until(store, rig, NOW, NOW + 30 * MINUTE) == []
    assert gate_log.recent(SLUG, None, tmp_path) == []
    assert kinds(store) == ["empty master seat"]


def test_a_merge_observation_outage_keeps_the_open_merge_alert(store, monkeypatch):
    enforce(store)
    rig = Rig()
    held_master(store, rig.runtime)
    stalled(monkeypatch)
    run(store, rig, NOW)
    monkeypatch.setattr(merge_stall, "observe", lambda repo, at: (_ for _ in ()).throw(OSError("GitHub down")))
    doc = {"tasks": [], "alerts": [{"id": "a1", "source": "merge", "state": "open"}]}
    assert run(store, rig, NOW + 5 * MINUTE, doc) == []
    assert rig.ledger.closed == []


def test_an_episode_logged_before_the_gate_moves_to_deny_raises_its_alert_then(store):
    rig = Rig()
    until(store, rig, NOW, NOW + 10 * MINUTE)
    assert rig.ledger.alerts == []
    enforce(store)
    run(store, rig, NOW + 11 * MINUTE)
    assert [source for source, _ in rig.ledger.alerts] == ["seat"]


def test_a_live_front_door_master_closes_the_seat_alert(store):
    enforce(store)
    rig = Rig()
    until(store, rig, NOW, NOW + 10 * MINUTE)
    held_master(store, rig.runtime)
    doc = {"tasks": [], "alerts": [{"id": "a1", "source": "seat", "state": "open"}]}
    assert run(store, rig, NOW + 11 * MINUTE, doc) == [
        "closed the seat alert: The night watch saw it clear: the front door master seat is held again"
    ]
    assert rig.ledger.closed == [("a1", "The night watch saw it clear: the front door master seat is held again")]
    assert kinds(store) == []


def test_a_gap_in_the_watch_starts_every_episode_over(store):
    enforce(store)
    rig = Rig()
    until(store, rig, NOW, NOW + 10 * MINUTE)
    doc = {"tasks": [], "alerts": [{"id": "a1", "source": "seat", "state": "open"}]}
    actions = run(store, rig, NOW + 60 * MINUTE, doc)
    assert actions == [f"closed the seat alert: {night_watch.GAP}"]
    assert rig.ledger.closed == [("a1", night_watch.GAP)]
    assert kinds(store) == [] and rig.respawned == [NOW + 10 * MINUTE]


@pytest.mark.parametrize("state, sleeping", [("paused", False), ("stopped", False), ("running", True)])
def test_a_seat_left_empty_on_purpose_is_never_judged(store, state, sleeping):
    store.update(SLUG, state=state)
    rig = Rig()
    until(store, rig, NOW, NOW + 60 * MINUTE, sleeping=sleeping)
    assert rig.respawned == [] and kinds(store) == []


def held_master(store, runtime):
    boss = AgentRecord("boss", "master", "master", state="working", seat="master@sw")
    store.put_agent(SLUG, boss)
    runtime.live.add("boss")


def ready(monkeypatch, tasks=1):
    rows, calls = [{"id": f"t{n}", "lane": "eng"} for n in range(tasks)], []

    def ready_work(slug, store, doc):
        calls.append((slug, store, doc))
        return {}, {"eng": rows, "ci": [], "plan": []}

    monkeypatch.setattr(capacity, "ready_work", ready_work)
    return calls


def decide(store, **decision):
    store.redis.set(store.key(SLUG, "quota-capacity"), json.dumps(decision))


def test_ready_work_held_by_host_room_raises_a_finding_naming_it_at_thirty_minutes(store, monkeypatch):
    enforce(store)
    rig = Rig()
    held_master(store, rig.runtime)
    ready(monkeypatch, 2)
    decide(store, host={"room": 0, "granted_at": NOW, "limit": 4, "reason": "load is high"})
    assert until(store, rig, NOW, NOW + 30 * MINUTE - 1) == []
    actions = run(store, rig, NOW + 30 * MINUTE)
    ((source, text),) = rig.ledger.alerts
    assert (rig.ledger.slugs, source) == ([SLUG], "spawn")
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
    until(store, rig, NOW, NOW + 30 * MINUTE)
    (finding,) = night_watch.findings(store, SLUG)
    assert finding.evidence == ("Hold: cap", "Evidence: eng 1 of 1 seats busy")
    assert rig.ledger.alerts == []


def test_ready_work_with_free_seats_and_no_placement_names_quota(store, monkeypatch):
    rig = Rig()
    held_master(store, rig.runtime)
    ready(monkeypatch)
    decide(store, configured={"eng": 1, "ci": 0, "plan": 0}, placements={"eng": []}, reason="accounts are drain")
    until(store, rig, NOW, NOW + 30 * MINUTE)
    (finding,) = night_watch.findings(store, SLUG)
    assert finding.evidence == ("Hold: quota", "Evidence: accounts are drain")


def test_ready_work_with_no_quota_decision_names_no_hold(store, monkeypatch):
    rig = Rig()
    held_master(store, rig.runtime)
    ready(monkeypatch)
    store.update(SLUG, max_eng=2)
    until(store, rig, NOW, NOW + 30 * MINUTE)
    (finding,) = night_watch.findings(store, SLUG)
    assert finding.evidence == ("Hold: no named hold", "Evidence: no quota decision")


def test_a_lane_without_configured_seats_counts_as_zero_seats_at_the_cap(store, monkeypatch):
    rig = Rig()
    held_master(store, rig.runtime)
    ready(monkeypatch)
    decide(store, configured={"ci": 0, "plan": 0}, placements={}, reason="accounts have quota")
    until(store, rig, NOW, NOW + 30 * MINUTE)
    (finding,) = night_watch.findings(store, SLUG)
    assert finding.evidence == ("Hold: cap", "Evidence: eng 0 of 0 seats busy")


def test_every_lane_at_its_cap_is_named_in_the_hold(store, monkeypatch):
    rig = Rig()
    held_master(store, rig.runtime)
    store.put_agent(SLUG, AgentRecord("e1", "eng", "engineer", state="working", started_at=NOW - MINUTE))
    rows = {"eng": [{"id": "t1", "lane": "eng"}], "ci": [{"id": "t2", "lane": "ci"}], "plan": []}
    monkeypatch.setattr(capacity, "ready_work", lambda slug, store, doc: ({}, rows))
    decide(store, configured={"eng": 1, "ci": 0, "plan": 0}, placements={}, reason="accounts have quota")
    until(store, rig, NOW, NOW + 30 * MINUTE)
    (finding,) = night_watch.findings(store, SLUG)
    assert finding.evidence == ("Hold: cap", "Evidence: eng 1 of 1 seats busy, ci 0 of 0 seats busy")


def test_ready_work_with_a_quota_placement_names_no_quota_hold(store, monkeypatch):
    rig = Rig()
    held_master(store, rig.runtime)
    store.put_agent(SLUG, AgentRecord("e1", "eng", "engineer", state="finished", started_at=NOW - MINUTE))
    ready(monkeypatch)
    decide(store, configured={"eng": 1, "ci": 0, "plan": 0}, placements={"eng": ["acct"]}, reason="eng is placed")
    until(store, rig, NOW, NOW + 30 * MINUTE)
    (finding,) = night_watch.findings(store, SLUG)
    assert finding.evidence == ("Hold: no named hold", "Evidence: eng is placed")


def test_ready_work_held_by_the_tick_spawn_hold_names_that_hold(store, monkeypatch):
    rig = Rig()
    held_master(store, rig.runtime)
    ready(monkeypatch)
    store.redis.set(store.key(SLUG, tick.SPAWN_HOLD), "holding spawns: panes are full")
    decide(store, configured={"eng": 1, "ci": 0, "plan": 0}, placements={"eng": ["acct"]}, reason="eng is placed")
    until(store, rig, NOW, NOW + 30 * MINUTE)
    (finding,) = night_watch.findings(store, SLUG)
    assert finding.evidence == ("Hold: tick", "Evidence: holding spawns: panes are full")


def test_a_quota_hold_without_a_reason_says_no_lane_is_placed(store, monkeypatch):
    rig = Rig()
    held_master(store, rig.runtime)
    ready(monkeypatch)
    decide(store, configured={"eng": 1, "ci": 0, "plan": 0}, placements={"eng": []})
    until(store, rig, NOW, NOW + 30 * MINUTE)
    (finding,) = night_watch.findings(store, SLUG)
    assert finding.evidence == ("Hold: quota", "Evidence: no lane has a quota placement")


def test_ready_work_while_the_swarm_sleeps_names_the_sleep(store, monkeypatch):
    rig = Rig()
    ready(monkeypatch)
    until(store, rig, NOW, NOW + 30 * MINUTE, sleeping=True)
    (finding,) = night_watch.findings(store, SLUG)
    assert finding.evidence[0] == "Hold: sleep"


def test_a_launch_restarts_the_held_spawn_clock(store, monkeypatch):
    rig = Rig()
    held_master(store, rig.runtime)
    ready(monkeypatch)
    until(store, rig, NOW, NOW + 16 * MINUTE)
    store.put_agent(SLUG, AgentRecord("e1", "eng", "engineer", state="working", started_at=NOW + 20 * MINUTE))
    until(store, rig, NOW + 20 * MINUTE, NOW + 30 * MINUTE)
    assert kinds(store) == []


def stalled(monkeypatch, **fields):
    observed = {**QUIET, "queue": 3, "merged": NOW - 61 * MINUTE, **fields}
    monkeypatch.setattr(merge_stall, "observe", lambda repo, at: {**observed, "at": at})


def test_an_enforced_merge_stall_alerts_and_emails_the_operator_once(store, monkeypatch):
    enforce(store)
    rig = Rig()
    held_master(store, rig.runtime)
    stalled(monkeypatch)
    actions = until(store, rig, NOW, NOW + 12 * MINUTE)
    ((source, text),) = rig.ledger.alerts
    assert source == "merge"
    assert text.startswith("Swarm sw dev merges are stuck: Dev has had no merge for 61 minutes with 3 queued entries")
    assert rig.mailed == [("Swarm sw: dev merges are stuck", text, "/repo")]
    assert actions[-1] == "emailed the operator: Dev has had no merge for 61 minutes with 3 queued entries"


def test_a_refused_email_says_so(store, monkeypatch):
    enforce(store)
    rig = Rig(refused="email failed: relay down")
    held_master(store, rig.runtime)
    stalled(monkeypatch)
    assert run(store, rig, NOW)[-1] == "email to the operator refused: email failed: relay down"


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


def test_a_second_merge_trigger_in_the_same_episode_replaces_the_alert(store, monkeypatch):
    enforce(store)
    rig = Rig()
    held_master(store, rig.runtime)
    stalled(monkeypatch)
    run(store, rig, NOW)
    stalled(monkeypatch, streak=3, head=99)
    doc = {"tasks": [], "alerts": [{"id": "a1", "source": "merge", "state": "open"}]}
    actions = run(store, rig, NOW + 5 * MINUTE, doc)
    assert actions[0] == "closed the merge alert: The night watch saw it clear: a new merge finding replaces it"
    assert len(rig.ledger.alerts) == 2 and len(rig.mailed) == 2
    assert "merge group runs failed in a row" in rig.ledger.alerts[1][1]


def test_a_logged_merge_stall_sends_no_email(store, monkeypatch, tmp_path):
    rig = Rig()
    held_master(store, rig.runtime)
    stalled(monkeypatch)
    actions = run(store, rig, NOW)
    assert rig.ledger.alerts == [] and rig.mailed == []
    assert actions[0].startswith("night watch gate not at deny: would raise the merge alert: ")
    assert [row["tool"] for row in gate_log.recent(SLUG, None, tmp_path)] == ["alert merge"]


def test_the_failing_streak_counts_newest_failures_past_cancelled_and_skipped_runs(monkeypatch):
    runs = [
        {"id": 6, "conclusion": "skipped"},
        {"id": 5, "conclusion": "cancelled"},
        {"id": 4, "conclusion": "failure"},
        {"id": 3, "conclusion": "timed_out"},
        {"id": 2, "conclusion": "success"},
        {"id": 1, "conclusion": "failure"},
    ]
    monkeypatch.setattr(merge_stall, "request", lambda repo, args: {"workflow_runs": runs})
    assert merge_stall.failing("/repo", "o/r") == (2, 4)


def sent(monkeypatch):
    from hooks.integrations import mailer

    mails = []

    def send_email(to, subject, body):
        mails.append((to, subject, body))
        return mailer.EmailResult(success=True, recipients_count=1)

    monkeypatch.setattr(mailer, "send_email", send_email)
    return mails


def test_the_operator_email_goes_to_the_configured_address(monkeypatch):
    mails = sent(monkeypatch)
    env = {operator_email.MAIL_TO: "boss@example.com"}
    assert operator_email.send("Subject", "Body", env, "/repo") == ""
    assert mails == [("boss@example.com", "Subject", "Body")]


def test_the_operator_email_falls_back_to_the_repository_git_user(monkeypatch, tmp_path):
    mails = sent(monkeypatch)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "owner@example.com"], check=True)
    assert operator_email.send("Subject", "Body", {}, str(tmp_path)) == ""
    assert mails == [("owner@example.com", "Subject", "Body")]


def test_no_operator_address_sends_nothing(monkeypatch, tmp_path):
    mails = sent(monkeypatch)
    assert operator_email.send("Subject", "Body", {}, str(tmp_path / "missing")) == (
        "no operator email: set AGENTIHOOKS_OPERATOR_EMAIL"
    )
    assert mails == []


def test_the_git_user_lookup_passes_a_five_second_timeout(monkeypatch):
    calls = []

    def found(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout="owner@example.com\n")

    monkeypatch.setattr(operator_email.subprocess, "run", found)
    assert operator_email._git_email("/repo") == "owner@example.com"
    assert calls == [
        (["git", "-C", "/repo", "config", "user.email"], {"capture_output": True, "text": True, "timeout": 5})
    ]


def test_a_failed_git_user_lookup_gives_no_address(monkeypatch):
    def fails(argv, **kwargs):
        raise OSError("no git")

    monkeypatch.setattr(operator_email.subprocess, "run", fails)
    assert operator_email._git_email("/repo") == ""


def test_night_watch_findings_reach_the_swarm_health_findings_the_doctor_reads(env):
    store, ledger, _ = env
    store.create(SwarmConfig(SLUG, "/repo", 1, 0, max_plan=0, scaling="manual"))
    episode = {"since": NOW, "minutes": 12, "why": "no master launched"}
    store.redis.set(store.key(SLUG, night_watch.KEY), json.dumps({"seat": episode}))
    found = status.findings(store, SLUG, store.config(SLUG), ledger.tasks(SLUG), [])
    assert "empty-master-seat/sw" in {row["id"] for row in found}


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


def test_the_respawn_runs_the_tick_master_launch_and_the_watch_gets_the_tick_inputs(store, monkeypatch):
    called, seen = [], {}
    monkeypatch.setattr(tick, "_master", lambda *args, **kwargs: called.append(args) or ["launched"])

    def run_watch(found):
        seen["watch"] = found
        return found.respawn()

    monkeypatch.setattr(night_watch, "run", run_watch)
    ledger, runtime, doc = Ledger(), Runtime(), {"tasks": [{"id": "t1"}]}
    assert tick._night_watch(SLUG, store, ledger, runtime, doc, NOW, True) == ["launched"]
    assert called == [(SLUG, store.config(SLUG), store, runtime, NOW)]
    found = seen["watch"]
    assert (found.store, found.slug, found.ledger, found.runtime, found.doc) == (store, SLUG, ledger, runtime, doc)
    assert (found.now_ms, found.sleeping) == (NOW, True)


def test_every_tick_runs_the_night_watch_with_its_ledger_and_sleep(monkeypatch):
    import fakeredis

    from tests.swarm.test_tick import FakeLedger, FakeRuntime

    swarm = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    swarm.create(SwarmConfig(SLUG, "/repo", max_eng=2, max_ci=1))
    seen = []
    monkeypatch.setattr(tick, "_night_watch", lambda *args: seen.append(args) or [])
    ledger, runtime = FakeLedger([{"id": "t1", "lane": "eng"}]), FakeRuntime()
    tick.tick(SLUG, swarm, ledger, runtime, now_ms=1_000)
    ((slug, store_, ledger_, runtime_, _, now, sleeping),) = seen
    assert (slug, store_, ledger_, runtime_, now, sleeping) == (SLUG, swarm, ledger, runtime, 1_000, False)


def plant(store, **episodes):
    store.redis.set(store.key(SLUG, night_watch.KEY), json.dumps({"at": NOW - MINUTE, **episodes}))


RAISED = {"since": NOW - 40 * MINUTE, "raised": NOW - MINUTE, "launch": 0, "merged": NOW - 61 * MINUTE}


def open_alert(source):
    return {"tasks": [], "alerts": [{"id": "a1", "source": source, "state": "open"}]}


def closes(store, rig, source, what, **extra):
    outcome = night_watch.CLEARED.format(what=what)
    assert run(store, rig, NOW, open_alert(source), **extra) == [f"closed the {source} alert: {outcome}"]
    assert rig.ledger.closed == [("a1", outcome)]
    return night_watch.read(store, SLUG).get(source)


def test_a_stopped_swarm_closes_its_spawn_alert(store):
    store.update(SLUG, state="paused")
    plant(store, spawn=RAISED)
    assert closes(store, Rig(), "spawn", "the swarm is not running") is None


def test_an_alert_already_done_is_never_closed_again(store):
    rig = Rig()
    store.update(SLUG, state="paused")
    plant(store, spawn=RAISED)
    doc = open_alert("spawn")
    doc["alerts"].insert(0, {"id": "a0", "source": "spawn", "state": "done"})
    outcome = night_watch.CLEARED.format(what="the swarm is not running")
    assert run(store, rig, NOW, doc) == [f"closed the spawn alert: {outcome}"]
    assert (rig.ledger.closed_slugs, rig.ledger.closed) == ([SLUG], [("a1", outcome)])


def test_ready_work_gone_closes_the_spawn_alert(store):
    rig = Rig()
    held_master(store, rig.runtime)
    plant(store, spawn=RAISED)
    assert closes(store, rig, "spawn", "no ready work waits") is None


def test_a_launch_closes_the_spawn_alert(store, monkeypatch):
    rig = Rig()
    held_master(store, rig.runtime)
    ready(monkeypatch)
    store.put_agent(SLUG, AgentRecord("e1", "eng", "engineer", state="working", started_at=NOW))
    plant(store, spawn=RAISED)
    assert closes(store, rig, "spawn", "an agent launched") == {"since": NOW, "launch": NOW}


def test_a_stopped_swarm_closes_its_merge_alert(store):
    store.update(SLUG, state="paused")
    plant(store, merge={**RAISED, "kinds": ["merge stall"]})
    assert closes(store, Rig(), "merge", "the swarm is not running") is None


def test_a_sleeping_swarm_closes_its_merge_alert(store, monkeypatch):
    stalled(monkeypatch)
    plant(store, merge={**RAISED, "kinds": ["merge stall"]})
    assert closes(store, Rig(), "merge", "the swarm is not running", sleeping=True) is None


def test_a_merge_that_lands_closes_the_merge_alert(store):
    rig = Rig()
    held_master(store, rig.runtime)
    plant(store, merge={**RAISED, "kinds": ["merge stall"]})
    assert closes(store, rig, "merge", "no merge finding stands") is None


def test_an_episode_without_kinds_starts_a_fresh_merge_alert(store, monkeypatch):
    enforce(store)
    rig = Rig()
    held_master(store, rig.runtime)
    stalled(monkeypatch)
    plant(store, merge={"merged": NOW - 61 * MINUTE, "raised": NOW - MINUTE})
    actions = run(store, rig, NOW, open_alert("merge"))
    assert actions[0] == f"closed the merge alert: {night_watch.CLEARED.format(what='a new merge finding replaces it')}"
    assert night_watch.read(store, SLUG)["merge"]["kinds"] == ["merge stall"]


def test_the_seat_findings_and_alert_carry_their_exact_words(store, tmp_path):
    enforce(store)
    rig = Rig()
    until(store, rig, NOW, NOW + 10 * MINUTE - 1)
    actions = run(store, rig, NOW + 10 * MINUTE)
    text = "The front door master seat of swarm sw has been empty for 10 minutes: no master launched"
    assert actions == ["respawned the master", f"raised the seat alert: {text}"]
    assert night_watch.findings(store, SLUG) == [
        Finding(
            "empty master seat",
            SLUG,
            "The front door master seat has been empty for 10 minutes",
            ("Why it stays empty: no master launched",),
            "ten minutes without a live front door master",
            10,
        )
    ]
    respawn = [row for row in gate_log.recent(SLUG, None, tmp_path) if row["tool"] == "respawn"]
    assert [row["reason"] for row in respawn] == ["front door master seat empty 10 minutes: no master launched"]


def test_a_held_master_spawn_names_why_the_seat_stays_empty(store):
    rig = Rig()
    store.redis.set(store.key(SLUG, "spawn-hold"), "holding the master spawn: host full")
    until(store, rig, NOW, NOW + 10 * MINUTE)
    (finding,) = night_watch.findings(store, SLUG)
    assert finding.evidence == ("Why it stays empty: holding the master spawn: host full",)


def test_a_refused_respawn_is_reported_with_its_cause(store):
    enforce(store)
    rig = Rig(respawns=[])
    actions = until(store, rig, NOW, NOW + 10 * MINUTE)
    assert actions[0] == "front door respawn refused: no master launched"


@pytest.mark.parametrize(
    "agent, live",
    [
        (AgentRecord("boss", "master", "master", state="starting", seat="master@sw"), True),
        (AgentRecord("boss", "master", "master", state="working", seat="master@sw"), False),
        (AgentRecord("boss", "master", "master", state="working", seat="master-2@sw"), True),
        (AgentRecord("boss", "eng", "engineer", state="working", seat="master@sw"), True),
    ],
)
def test_only_a_live_working_master_on_the_front_door_holds_the_seat(store, agent, live):
    rig = Rig()
    store.put_agent(SLUG, agent)
    if live:
        rig.runtime.live.add(agent.name)
    until(store, rig, NOW, NOW + 10 * MINUTE)
    assert kinds(store) == ["empty master seat"]


def test_the_spawn_finding_and_alert_carry_their_exact_words(store, monkeypatch):
    enforce(store)
    rig = Rig()
    held_master(store, rig.runtime)
    calls = ready(monkeypatch, 2)
    decide(store, host={"room": 0, "granted_at": NOW, "limit": 4, "reason": "load is high"})
    until(store, rig, NOW, NOW + 30 * MINUTE)
    assert calls[0] == (SLUG, store, {"tasks": []})
    why = "host 4 room 0, 0 spawned since it was granted: load is high"
    assert rig.ledger.alerts == [
        ("spawn", f"Swarm sw has had 2 ready tasks and no agent launch for 30 minutes, held by host room: {why}")
    ]
    assert night_watch.findings(store, SLUG) == [
        Finding(
            "held spawn",
            SLUG,
            "2 ready tasks and no agent launch for 30 minutes, held by host room",
            ("Hold: host room", f"Evidence: {why}"),
            "thirty minutes of ready work without a launch",
            30,
        )
    ]


def test_the_merge_alert_joins_every_finding_and_mails_with_the_watch_environment(store, monkeypatch):
    enforce(store)
    rig = Rig()
    held_master(store, rig.runtime)
    repos = []
    observed = {**QUIET, "queue": 3, "merged": NOW - 61 * MINUTE, "streak": 3, "head": 99}
    monkeypatch.setattr(merge_stall, "observe", lambda repo, at: repos.append(repo) or {**observed, "at": at})
    env = {"AGENTIHOOKS_OPERATOR_EMAIL": "boss@example.com"}
    run(store, rig, NOW, environ=env)
    summary = (
        "Dev has had no merge for 61 minutes with 3 queued entries; "
        "The last 3 merge group runs failed in a row with 3 queued entries"
    )
    evidence = (
        "Queue length: 3; Time since last dev merge: 61 minutes; "
        "Most common failed check in the last 10 queue runs: none observed; "
        "Queue length: 3; Newest failed merge group run: 99"
    )
    text = f"Swarm sw dev merges are stuck: {summary}. {evidence}"
    assert rig.ledger.alerts == [("merge", text)]
    assert rig.mailed == [("Swarm sw: dev merges are stuck", text, "/repo")]
    assert rig.envs == [env] and repos == ["/repo"]


@pytest.mark.parametrize("streak, lead", [(3, ""), (9, ""), (10, "at least ")])
def test_the_merge_failures_finding_carries_its_exact_words(store, monkeypatch, streak, lead):
    stalled(monkeypatch, merged=NOW - 5 * MINUTE, streak=streak, head=99)
    assert merge_stall.findings(store, SLUG, "/repo", NOW) == [
        Finding(
            "merge failures",
            "o/r",
            f"The last {lead}{streak} merge group runs failed in a row with 3 queued entries",
            ("Queue length: 3", "Newest failed merge group run: 99"),
            "3 merge group runs failing in a row",
            streak,
        )
    ]


def test_the_merge_failures_finding_stands_with_an_empty_dev_queue(store, monkeypatch):
    stalled(monkeypatch, queue=0, merged=0, streak=3, head=99)
    assert merge_stall.findings(store, SLUG, "/repo", NOW) == [
        Finding(
            "merge failures",
            "o/r",
            "The last 3 merge group runs failed in a row with 0 queued entries",
            ("Queue length: 0", "Newest failed merge group run: 99"),
            "3 merge group runs failing in a row",
            3,
        )
    ]
