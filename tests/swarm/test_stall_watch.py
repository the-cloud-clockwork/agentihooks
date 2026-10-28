import copy
import json
from dataclasses import replace

import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm import idle, ledger_events, outage, ready, release_wait, stall_watch, waits
from scripts.swarm.health.findings import Finding
from scripts.swarm.ledger_events import PullRequest
from scripts.swarm.stall_watch import ALERTED, REOPENED
from scripts.swarm.store import MASTER, AgentRecord, RedisStore, SwarmConfig
from scripts.swarm_v2 import masters
from scripts.swarm_v2.masters import MasterSeats
from tests.swarm.test_tick import FakeLedger

SLUG = "sw"
URL = "https://github.com/o/r/pull/7"
OWNER = "engineer@abc-0001"
MINUTE = 60_000
START = 10_000_000
GREEN = PullRequest("OPEN", None, START, False, True, head="h")
RED = PullRequest("OPEN", None, START, True, True, head="h", red_at=START)
RUNNING = PullRequest("OPEN", None, START, False, False, head="h")
QUEUED = PullRequest("OPEN", None, START, False, True, head="h", queued=True)
CONFLICTING = PullRequest("OPEN", None, START, False, False, head="h", mergeable="CONFLICTING")
DRAFT = PullRequest("OPEN", None, START, False, True, head="h", draft=True)
LEAD = masters.lead_of(SLUG, [])
UNCLAIMED_ALERT = {"case": "unclaimed", "minutes": 15, "wait": 30, "pr": "none", "owner": "nobody", "action": ALERTED}


class Ledger(FakeLedger):
    def __init__(self, tasks):
        super().__init__(tasks)
        self.priorities, self.updates = [], []

    def task(self, slug, task_id):
        assert slug == SLUG
        return super().task(slug, task_id)

    def update_task(self, slug, task_id, fields, by="swarm", if_state=()):
        self.updates.append((task_id, fields, if_state))
        return super().update_task(slug, task_id, fields, by, if_state)

    def comment(self, slug, task, text, by):
        assert slug == SLUG
        self.comments.append((task, text, by))

    def priority(self, slug, item, text):
        assert slug == SLUG
        self.priorities.append((item, text))


class Swarm:
    def __init__(self, pull=GREEN, task=None, live=True, environ=None):
        import fakeredis

        self.store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
        self.store.create(SwarmConfig(SLUG, "/repo", 1, 1, gates={"stall": "enforce"}))
        self.ledger = Ledger(
            [{"id": "t1", "state": "pr", "pr_url": URL, "claimed_by": OWNER, "branch": "b1", **(task or {})}]
        )
        self.pulls = {URL: pull}
        self.retired, self.posted, self.envs, self.refusal = [], [], [], ""
        self.ready = {"eng": [], "ci": []}
        self.live = {OWNER} if live else set()
        self.environ = {stall_watch.NTFY: "https://ntfy.example/alerts", **(environ or {})}
        self.store.put_agent(SLUG, AgentRecord(OWNER, "eng", "t1", seat="eng-1@sw"))
        self.store.redis.set(self.store.key(SLUG, "claim", "t1"), OWNER)

    def tick(self, at, doc=None, **extra):
        watch = stall_watch.Watch(
            self.store,
            SLUG,
            self.ledger,
            self.pulls.get,
            frozenset(self.live),
            lambda agent: self.retired.append(agent.name) or True,
            lambda lane: self.ready[lane],
            at,
            self.environ,
            lambda url, title, text, env: (
                self.posted.append((url, title, text)) or self.envs.append(env) or self.refusal
            ),
            **extra,
        )
        return stall_watch.stall_pass(watch, doc or self.ledger.state(SLUG))

    def inbox(self):
        return InboxStore(self.store.redis).inbox("master@sw")


def _reopened(swarm):
    row = swarm.ledger.rows["t1"]
    return row["state"] == "open" and row["claimed_by"] == "" and row["branch"] == "b1"


def test_limits_default_to_the_operator_order_and_read_settings():
    assert stall_watch.limits({}) == stall_watch.Limits(green=10, red=15, owner=10, unmergeable=5, unclaimed=15)
    assert stall_watch.limits({"AGENTIHOOKS_STALL_GREEN_MINUTES": "3", "AGENTIHOOKS_STALL_RED_MINUTES": "x"}) == (
        stall_watch.Limits(green=3)
    )
    assert stall_watch.limits({"AGENTIHOOKS_STALL_GREEN_MINUTES": "0", "AGENTIHOOKS_STALL_RED_MINUTES": "1"}) == (
        stall_watch.Limits(red=1)
    )


def test_a_green_pull_request_left_unqueued_past_its_limit_reopens_within_two_ticks():
    swarm = Swarm(GREEN)
    assert swarm.tick(START) == []
    assert swarm.tick(START + 9 * MINUTE) == []
    actions = swarm.tick(START + 10 * MINUTE)
    assert _reopened(swarm) and swarm.retired == [OWNER]
    assert [a.name for a in swarm.store.agents(SLUG) if a.state != "finished"] == []
    assert swarm.store.claimant(SLUG, "t1") is None
    assert any("green and not queued for 10 minutes" in a for a in actions)
    [item] = swarm.inbox()
    assert URL in item.text and OWNER in item.text and "10 minutes" in item.text and "reopened" in item.text
    assert "green and not queued" in swarm.ledger.comments[0][1]


def test_a_queued_or_draft_or_running_pull_request_is_not_a_green_stall():
    for pull in (QUEUED, DRAFT, RUNNING):
        swarm = Swarm(pull)
        swarm.tick(START)
        assert swarm.tick(START + 30 * MINUTE) == [] and not _reopened(swarm)


def test_a_red_pull_request_with_no_push_past_its_limit_reopens():
    swarm = Swarm(RED)
    assert swarm.tick(START + 14 * MINUTE) == []
    swarm.tick(START + 15 * MINUTE)
    assert _reopened(swarm)
    assert "red with no push for 15 minutes" in swarm.inbox()[0].text


def test_a_watchdog_reopen_marks_the_next_start_as_after_the_watchdog():
    swarm = Swarm(RED)
    swarm.tick(START + 14 * MINUTE)
    assert swarm.store.take_restart(SLUG, "t1") == ""
    swarm.tick(START + 15 * MINUTE)
    assert _reopened(swarm)
    assert swarm.store.take_restart(SLUG, "t1") == "watchdog"


@pytest.mark.parametrize("newest", ["CANCELLED", "FAILURE"])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("same_time", [False, True])
@pytest.mark.parametrize("missing_older_id", [False, True])
def test_the_newest_run_cancellation_holds_a_red_watchdog(newest, reverse, same_time, missing_older_id):
    suites = [
        {
            "status": "COMPLETED",
            "conclusion": "FAILURE" if newest == "CANCELLED" else "CANCELLED",
            "workflowRun": {
                "databaseId": 1,
                "createdAt": "1970-01-01T02:46:40Z" if same_time else "1970-01-01T02:45:00Z",
            },
        },
        {
            "status": "COMPLETED",
            "conclusion": newest,
            "workflowRun": {"databaseId": 2, "createdAt": "1970-01-01T02:46:40Z"},
        },
    ]
    if missing_older_id:
        suites[0]["workflowRun"].pop("databaseId")
        suites[1]["workflowRun"]["databaseId"] = 1
    pull = ledger_events.pull_request(
        {
            "state": "OPEN",
            "gated": True,
            "checkSuites": list(reversed(suites)) if reverse else suites,
            "statusCheckRollup": [
                {"name": ledger_events.GATE, "conclusion": "FAILURE", "completedAt": "1970-01-01T02:46:40Z"}
            ],
        }
    )
    swarm = Swarm(pull)
    swarm.tick(START + 15 * MINUTE)
    swarm.tick(START + 30 * MINUTE)
    assert _reopened(swarm) == (newest == "FAILURE")
    assert swarm.retired == ([OWNER] if newest == "FAILURE" else [])


def test_a_draft_red_pull_request_keeps_its_owner():
    swarm = Swarm(replace(RED, draft=True))
    assert swarm.tick(START + 15 * MINUTE) == []
    assert swarm.tick(START + 30 * MINUTE) == []
    assert not _reopened(swarm)
    assert swarm.retired == []


@pytest.mark.parametrize("pull", [RED, GREEN, CONFLICTING])
def test_a_new_owner_gets_the_full_limit_after_hours_without_a_push(pull):
    swarm = Swarm(pull, live=False)
    swarm.tick(START)
    claim = START + 180 * MINUTE
    swarm.store.put_agent(SLUG, AgentRecord(OWNER, "eng", "t1", started_at=claim, seat="eng-1@sw"))
    assert swarm.tick(claim + MINUTE) == []
    assert swarm.ledger.rows["t1"]["state"] == "pr"
    assert swarm.retired == []
    assert swarm.inbox() == []
    limit = 15 if pull.red else 10 if pull == GREEN else 5
    assert swarm.tick(claim + (limit - 1) * MINUTE) == []
    swarm.tick(claim + limit * MINUTE)
    assert _reopened(swarm)


def test_red_checks_in_the_merge_queue_never_reopen_the_task():
    swarm = Swarm(replace(RED, queued=True))
    assert swarm.tick(START + 180 * MINUTE) == []
    assert swarm.ledger.rows["t1"]["state"] == "pr"
    assert swarm.retired == []
    assert swarm.inbox() == []


@pytest.mark.parametrize("pull,state", [(replace(CONFLICTING, draft=True), "starting"), (CONFLICTING, "working")])
def test_unmergeable_drafts_and_live_working_owners_stay_held(pull, state):
    swarm = Swarm(pull)
    swarm.store.put_agent(SLUG, AgentRecord(OWNER, "eng", "t1", state=state, seat="eng-1@sw"))
    assert swarm.tick(START) == []
    assert swarm.tick(START + 180 * MINUTE) == []
    assert swarm.ledger.rows["t1"]["state"] == "pr"
    assert swarm.retired == []
    assert swarm.inbox() == []


def test_a_push_after_red_restarts_the_red_limit():
    swarm = Swarm(PullRequest("OPEN", None, START + 10 * MINUTE, True, True, head="h", red_at=START))
    assert swarm.tick(START + 20 * MINUTE) == [] and not _reopened(swarm)


def test_an_owner_not_live_past_its_limit_reopens_and_nothing_is_retired():
    swarm = Swarm(RUNNING, live=False)
    swarm.store.drop_agent(SLUG, OWNER)
    swarm.tick(START)
    assert not _reopened(swarm)
    swarm.tick(START + 10 * MINUTE)
    assert _reopened(swarm) and swarm.retired == []
    assert "owner not live for 10 minutes" in swarm.inbox()[0].text


def test_a_task_awaiting_the_merge_pass_has_no_owner_to_lose():
    swarm = Swarm(RUNNING, task={"awaiting": ready.AWAITING}, live=False)
    swarm.tick(START)
    assert swarm.tick(START + 30 * MINUTE) == [] and not _reopened(swarm)


@pytest.mark.parametrize("pull", [GREEN, RUNNING, QUEUED])
def test_a_released_pull_request_stays_held_past_the_stall_limits(pull):
    swarm = Swarm(pull, task={"awaiting": ready.AWAITING}, live=False)
    swarm.store.drop_agent(SLUG, OWNER)
    swarm.store.release(SLUG, "t1", OWNER)
    for minutes in (0, 10, 30, 60):
        assert swarm.tick(START + minutes * MINUTE) == []
        assert swarm.ledger.rows["t1"]["state"] == "pr"
        assert swarm.ledger.rows["t1"]["awaiting"] == ready.AWAITING
    assert swarm.ledger.updates == []
    assert swarm.retired == []
    assert swarm.inbox() == []


def test_a_released_dependency_wait_does_not_hide_another_stalled_pull_request():
    swarm = Swarm(RED, task={"awaiting": ready.AWAITING}, live=False)
    other = URL + "0"
    swarm.ledger.rows["t2"] = {"id": "t2", "state": "pr", "pr_url": other, "claimed_by": ""}
    swarm.ledger.rows["repair"] = {"id": "repair", "state": "pr"}
    swarm.pulls[other] = RED
    found = ready.held_record(URL, "h", "", OWNER, START)
    found["dependency"] = {"kind": "task", "target": "repair"}
    ready.save(swarm.store, SLUG, "t1", found)
    swarm.tick(START + 30 * MINUTE)
    assert swarm.ledger.rows["t1"]["state"] == "pr"
    assert swarm.ledger.rows["t2"]["state"] == "open"


@pytest.mark.parametrize("kind, held", [("recover", True), ("merge", False)])
def test_a_transient_unmergeable_requeue_backoff_holds_the_dropped_queue_reopen(kind, held):
    swarm = Swarm(QUEUED, task={"awaiting": ready.AWAITING}, live=False)
    found = ready.held_record(URL, "h", "", OWNER, START)
    found["on"] = {"kind": kind, "target": URL, "head": "h"}
    ready.save(swarm.store, SLUG, "t1", found)
    swarm.tick(START)
    swarm.pulls[URL] = GREEN
    swarm.tick(START + MINUTE)
    swarm.tick(START + 30 * MINUTE)
    assert (swarm.ledger.rows["t1"]["state"] == "pr") is held


@pytest.mark.parametrize("pull", [GREEN, RUNNING])
def test_a_ready_release_since_the_snapshot_prevents_generic_stall_recovery(pull):
    swarm = Swarm(pull, live=False)
    assert swarm.tick(START) == []
    before = copy.deepcopy(swarm.ledger.state(SLUG))
    swarm.ledger.rows["t1"]["awaiting"] = ready.AWAITING
    assert swarm.tick(START + 30 * MINUTE, before) == []
    assert swarm.ledger.rows["t1"]["awaiting"] == ready.AWAITING
    assert swarm.ledger.updates == []
    assert swarm.retired == []


def test_an_unmergeable_pull_request_reopens_after_five_minutes():
    swarm = Swarm(CONFLICTING, live=False)
    swarm.tick(START)
    assert not _reopened(swarm)
    swarm.tick(START + 5 * MINUTE)
    assert _reopened(swarm)
    assert "unmergeable or dropped from the queue for 5 minutes" in swarm.inbox()[0].text


def test_a_pull_request_dropped_from_the_queue_reopens_after_five_minutes():
    swarm = Swarm(QUEUED, live=False)
    swarm.tick(START)
    swarm.pulls[URL] = RUNNING
    swarm.tick(START + MINUTE)
    assert not _reopened(swarm)
    swarm.tick(START + 6 * MINUTE)
    assert _reopened(swarm)


def test_a_refused_retire_holds_the_task_for_the_next_tick():
    swarm = Swarm(GREEN)
    swarm.tick(START)
    watch = stall_watch.Watch(
        swarm.store,
        SLUG,
        swarm.ledger,
        swarm.pulls.get,
        frozenset({OWNER}),
        lambda agent: False,
        lambda lane: [],
        START + 10 * MINUTE,
        swarm.environ,
        lambda *a: "",
    )
    actions = stall_watch.stall_pass(watch, swarm.ledger.state(SLUG))
    assert not _reopened(swarm) and swarm.inbox() == []
    assert actions == [f"could not retire {OWNER} for stalled task t1, retrying next tick"]


def _full_lane(swarm, free=1):
    swarm.ledger.rows["t1"].update(state="open", claimed_by="", pr_url="")
    swarm.ledger.rows["t2"] = {"id": "t2", "state": "claimed", "claimed_by": OWNER, "lane": "eng"}
    swarm.ready["eng"] = [swarm.ledger.rows["t1"]]
    decision = {"configured": {"eng": 1, "ci": 1, "plan": 0}, "placeable": {"claude": free, "codex": 0}}
    swarm.store.redis.set(swarm.store.key(SLUG, "quota-capacity"), json.dumps(decision))


def test_a_ready_task_unclaimed_while_the_lane_is_full_and_seats_are_free_alerts_only():
    swarm = Swarm(RUNNING)
    _full_lane(swarm)
    swarm.tick(START)
    assert swarm.inbox() == []
    swarm.tick(START + 15 * MINUTE)
    [item] = swarm.inbox()
    assert "unclaimed while its lane is full for 15 minutes" in item.text
    assert swarm.retired == [] and swarm.ledger.rows["t1"]["state"] == "open"
    swarm.tick(START + 16 * MINUTE)
    assert len(swarm.inbox()) == 1


def test_no_free_seat_means_the_unclaimed_task_is_not_a_stall():
    swarm = Swarm(RUNNING)
    _full_lane(swarm, free=0)
    swarm.tick(START)
    assert swarm.tick(START + 30 * MINUTE) == [] and swarm.inbox() == []


def test_an_alert_unanswered_two_more_limits_reaches_priorities_and_ntfy_once():
    swarm = Swarm(GREEN)
    swarm.tick(START)
    swarm.tick(START + 10 * MINUTE)
    swarm.tick(START + 29 * MINUTE)
    assert swarm.ledger.priorities == [] and swarm.posted == []
    swarm.tick(START + 30 * MINUTE)
    [(item, text)] = swarm.ledger.priorities
    assert item == "tasks/t1" and URL in text
    [(url, _, body)] = swarm.posted
    assert url == "https://ntfy.example/alerts" and URL in body
    swarm.tick(START + 40 * MINUTE)
    assert len(swarm.ledger.priorities) == 1 and len(swarm.posted) == 1


def test_an_answered_alert_never_escalates_and_its_finding_clears():
    swarm = Swarm(GREEN)
    swarm.tick(START)
    swarm.tick(START + 10 * MINUTE)
    assert stall_watch.findings(swarm.store, SLUG) == [
        Finding(
            "stall",
            "t1",
            "green and not queued for 10 minutes",
            (f"pull request {URL}", f"owner {OWNER}", f"action: {REOPENED}"),
            "10 minutes green and not queued",
            10,
        )
    ]
    InboxStore(swarm.store.redis).close(swarm.inbox()[0].id, "master@sw", "done", "handled")
    swarm.tick(START + 30 * MINUTE)
    assert swarm.ledger.priorities == [] and stall_watch.findings(swarm.store, SLUG) == []


def test_without_an_ntfy_setting_escalation_posts_to_the_anton_alerts_topic():
    swarm = Swarm(GREEN)
    del swarm.environ[stall_watch.NTFY]
    swarm.tick(START)
    swarm.tick(START + 10 * MINUTE)
    swarm.tick(START + 30 * MINUTE)
    assert len(swarm.ledger.priorities) == 1
    assert [url for url, _, _ in swarm.posted] == ["https://ntfy.homeofanton.com/anton-alerts"]


def test_ntfy_signs_in_with_the_access_audit_credentials_and_reports_a_refusal():
    sent = []

    class Opened:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def opener(request, timeout):
        sent.append((request, timeout))
        return Opened()

    env = {"NTFY_USER": "alerts", "NTFY_PASS": "pw"}
    assert stall_watch.ntfy("https://ntfy.example/t", "Stall", "body", env, opener) == ""
    request, timeout = sent[0]
    assert request.headers == {"Title": "Stall", "Authorization": "Basic YWxlcnRzOnB3"}
    assert request.get_method() == "POST" and request.data == b"body" and timeout == 10
    for partial in ({}, {"NTFY_USER": "alerts"}, {"NTFY_PASS": "pw"}):
        assert stall_watch.ntfy("https://ntfy.example/t", "Stall", "body", partial, opener) == ""
        assert sent[-1][0].headers == {"Title": "Stall"}

    def refused(request, timeout):
        raise OSError("HTTP Error 401: Unauthorized")

    assert stall_watch.ntfy("https://ntfy.example/t", "Stall", "body", env, refused) == (
        "ntfy failed: HTTP Error 401: Unauthorized"
    )


def test_stall_findings_are_not_sent_twice_by_the_findings_pass():
    swarm = Swarm(GREEN)
    found = {"kind": "stall", "id": "stall/t1", "subject": "t1", "summary": "s", "verdict": None}
    inbox = InboxStore(swarm.store.redis)
    assert ledger_events.findings_pass(inbox, swarm.store, SLUG, [found], swarm.ledger.state(SLUG)) == []


def test_the_pull_request_view_reads_mergeable_and_draft():
    assert "mergeable isDraft" in ledger_events.PULL_QUERY
    raw = {"state": "OPEN", "mergeable": "CONFLICTING", "isDraft": True, "commits": [], "statusCheckRollup": []}
    pull = ledger_events.pull_request(raw)
    assert pull.mergeable == "CONFLICTING" and pull.draft is True
    unknown = ledger_events.pull_request({key: value for key, value in raw.items() if key != "mergeable"})
    assert unknown.mergeable == "" and unknown.draft is True


def test_a_task_the_merge_pass_already_reopened_is_left_alone():
    swarm = Swarm(GREEN)
    swarm.tick(START)
    before = copy.deepcopy(swarm.ledger.state(SLUG))
    swarm.ledger.rows["t1"].update(state="open", claimed_by="")
    assert swarm.tick(START + 10 * MINUTE, before) == []
    assert swarm.retired == [] and swarm.ledger.comments == [] and swarm.inbox() == []


def test_a_registered_owner_that_is_not_live_is_reopened_without_a_retire():
    swarm = Swarm(RUNNING, live=False)
    swarm.tick(START)
    swarm.tick(START + 10 * MINUTE)
    assert _reopened(swarm) and swarm.retired == []
    assert [a.name for a in swarm.store.agents(SLUG) if a.state != "finished"] == []


@pytest.mark.parametrize("age", [0, 179_999, 180_000, 180_001])
def test_owner_liveness_uses_the_hive_join_heartbeat_window(age):
    from scripts.hive import join_report

    swarm = Swarm(RUNNING, live=False)
    owner = replace(swarm.store.agents(SLUG)[0], hive="w7")
    swarm.store.put_agent(SLUG, owner)
    for at in (START, START + 10 * MINUTE):
        join_report.start(swarm.store.redis, SLUG, owner, str(at), {}, {}, at - age)
        swarm.tick(at)
    expired = age >= join_report.HEARTBEAT_MS
    assert bool(_reopened(swarm)) == expired
    assert bool(swarm.inbox()) == expired
    if expired:
        assert "owner not live for 10 minutes" in swarm.inbox()[0].text
    assert swarm.retired == []


def test_an_outage_holds_the_reopen(monkeypatch):
    monkeypatch.setattr(stall_watch.outage, "held", lambda store, slug, name: "ledger health probe failed")
    swarm = Swarm(GREEN)
    swarm.tick(START)
    assert swarm.tick(START + 30 * MINUTE) == [] and not _reopened(swarm) and swarm.retired == []


def test_outage_time_does_not_count_toward_a_limit_or_the_escalation(monkeypatch):
    swarm = Swarm(GREEN)
    swarm.tick(START)
    monkeypatch.setattr(stall_watch.outage, "paused_ms", lambda slug, since, now: 5 * MINUTE)
    assert swarm.tick(START + 14 * MINUTE) == [] and not _reopened(swarm)
    swarm.tick(START + 15 * MINUTE)
    assert _reopened(swarm)
    swarm.tick(START + 39 * MINUTE)
    assert swarm.ledger.priorities == []
    swarm.tick(START + 40 * MINUTE)
    assert len(swarm.ledger.priorities) == 1


def test_a_red_pull_request_with_no_marks_is_not_a_red_stall():
    swarm = Swarm(PullRequest("OPEN", None, None, True, True, head="h"))
    swarm.tick(START)
    assert swarm.tick(START + 30 * MINUTE) == [] and not _reopened(swarm)


def test_a_closed_unclaimed_alert_is_not_sent_again_while_the_task_still_waits():
    swarm = Swarm(RUNNING)
    _full_lane(swarm)
    swarm.tick(START)
    swarm.tick(START + 15 * MINUTE)
    InboxStore(swarm.store.redis).close(swarm.inbox()[0].id, "master@sw", "done", "seen")
    assert swarm.tick(START + 16 * MINUTE) == []
    assert swarm.tick(START + 17 * MINUTE) == []


def test_the_tick_runs_the_stall_pass_after_the_merge_pass():
    import inspect

    from scripts.swarm import cli

    source = inspect.getsource(cli.run_tick)
    assert source.index("ready.merge_pass") < source.index("stall_watch.stall_pass")


def _alert(**fields):
    row = {"slug": SLUG, "task": "t1", "title": "no title", "pr": URL, "owner": OWNER, "action": REOPENED, **fields}
    return stall_watch.ALERT.format(**{**row, "case": stall_watch.CASES[row["case"]]})


def _queued(swarm):
    return swarm.store.redis.smembers(swarm.store.key(SLUG, "stall-queued"))


def _alert_rows(swarm):
    return swarm.store.redis.hgetall(swarm.store.key(SLUG, "stall-alerts"))


def test_a_reopen_writes_the_exact_ledger_update_comment_alert_and_seat_exit():
    swarm = Swarm(GREEN, task={"title": "Fix it"})
    swarm.tick(START)
    actions = swarm.tick(START + 10 * MINUTE)
    assert actions == [f"stall: task t1 green and not queued for 10 minutes, {REOPENED}; told {LEAD}"]
    assert swarm.ledger.updates == [("t1", {"state": "open", "claimed_by": "", "awaiting": ""}, stall_watch.HELD)]
    comment = stall_watch.COMMENT.format(action=REOPENED, pr=URL, case="green and not queued", minutes=10)
    assert swarm.ledger.comments == [("t1", comment, "swarm")]
    [item] = swarm.inbox()
    assert item.text == _alert(case="green", minutes=10, wait=20, title="Fix it")
    assert (item.sender, item.ref) == ("swarm", "sw:stall:t1")
    exit_record = InboxStore(swarm.store.redis).seats.exit_of(OWNER)
    assert (exit_record["seat"], exit_record["reason"]) == ("eng-1@sw", "stopped")


def test_the_watch_settings_set_the_limits():
    swarm = Swarm(GREEN, environ={"AGENTIHOOKS_STALL_GREEN_MINUTES": "3"})
    swarm.tick(START)
    swarm.tick(START + 3 * MINUTE)
    assert _reopened(swarm)


def test_an_alert_goes_to_the_master_owning_the_task_phase():
    swarm = Swarm(GREEN, task={"phase": "p2"})
    swarm.ledger.phases = [{"id": "p1"}, {"id": "p2"}]
    MasterSeats(swarm.store.redis).set_count(SLUG, 2)
    seats = masters.seats(SLUG, 2)
    for index, seat in enumerate(seats):
        swarm.store.put_agent(SLUG, AgentRecord(f"master@abc-000{index + 5}", MASTER, "", seat=seat))
    swarm.tick(START)
    [action] = swarm.tick(START + 10 * MINUTE)
    assert action.endswith(f"; told {seats[1]}")


def test_a_closed_pull_request_clears_its_queue_mark():
    swarm = Swarm(QUEUED)
    swarm.tick(START)
    assert _queued(swarm) == {"t1"}
    swarm.pulls[URL] = replace(QUEUED, state="MERGED", queued=False)
    assert swarm.tick(START + MINUTE) == []
    assert _queued(swarm) == set()


def test_a_task_that_leaves_review_drops_its_queue_mark():
    swarm = Swarm(QUEUED)
    swarm.tick(START)
    swarm.ledger.rows["t1"]["state"] = "done"
    swarm.tick(START + MINUTE)
    assert _queued(swarm) == set()


def test_a_reopen_after_a_queue_drop_clears_its_queue_mark():
    swarm = Swarm(QUEUED, live=False)
    swarm.tick(START)
    swarm.pulls[URL] = RUNNING
    swarm.tick(START + MINUTE)
    swarm.tick(START + 6 * MINUTE)
    assert _reopened(swarm) and _queued(swarm) == set()


def test_two_stalled_pull_requests_act_in_the_same_tick():
    other, url = "engineer@abc-0002", "https://github.com/o/r/pull/8"
    swarm = Swarm(GREEN)
    swarm.ledger.rows["t2"] = {**swarm.ledger.rows["t1"], "id": "t2", "pr_url": url, "claimed_by": other}
    swarm.pulls[url] = GREEN
    swarm.live.add(other)
    swarm.store.put_agent(SLUG, AgentRecord(other, "eng", "t2", seat="eng-2@sw"))
    swarm.tick(START)
    assert len(swarm.tick(START + 10 * MINUTE)) == 2
    assert sorted(swarm.retired) == [OWNER, other]


def test_a_stall_mark_is_forgotten_once_the_pull_request_stops_stalling():
    swarm = Swarm(GREEN)
    swarm.tick(START)
    swarm.pulls[URL] = RUNNING
    swarm.tick(START + 5 * MINUTE)
    assert swarm.store.redis.hgetall(swarm.store.key(SLUG, "stall-seen")) == {}
    swarm.pulls[URL] = GREEN
    swarm.tick(START + 6 * MINUTE)
    assert swarm.tick(START + 15 * MINUTE) == [] and not _reopened(swarm)
    swarm.tick(START + 16 * MINUTE)
    assert _reopened(swarm)


def test_outage_time_is_measured_over_the_stall_itself(monkeypatch):
    calls = []

    def paused(slug, since, now):
        calls.append((slug, since, now))
        return (now - since) // 2

    monkeypatch.setattr(stall_watch.outage, "paused_ms", paused)
    swarm = Swarm(GREEN)
    swarm.tick(START)
    swarm.tick(START + 19 * MINUTE)
    assert not _reopened(swarm)
    swarm.tick(START + 20 * MINUTE)
    assert _reopened(swarm) and (SLUG, START, START + 20 * MINUTE) in calls


@pytest.mark.parametrize("nobody", ["", None])
def test_a_red_pull_request_with_no_owner_reopens_and_names_nobody(nobody):
    swarm = Swarm(RED, task={"claimed_by": nobody})
    swarm.tick(START + 15 * MINUTE)
    assert _reopened(swarm) and swarm.retired == []
    assert swarm.inbox()[0].text == _alert(case="red", minutes=15, wait=30, owner="nobody")


def test_a_provider_error_on_the_owner_pane_holds_its_reopen():
    swarm = Swarm(GREEN)
    swarm.store.redis.set(swarm.store.key(SLUG, outage.PANE, OWNER), "provider error")
    swarm.tick(START)
    assert swarm.tick(START + 10 * MINUTE) == [] and not _reopened(swarm) and swarm.retired == []


def test_a_task_finished_since_the_snapshot_is_left_alone():
    swarm = Swarm(GREEN)
    swarm.tick(START)
    before = copy.deepcopy(swarm.ledger.state(SLUG))
    swarm.ledger.rows["t1"]["state"] = "done"
    assert swarm.tick(START + 10 * MINUTE, before) == []
    assert swarm.retired == [] and swarm.ledger.updates == []


def test_a_finished_owner_record_is_not_retired_and_no_other_agent_is():
    other = "engineer@abc-0000"
    swarm = Swarm(GREEN)
    swarm.store.put_agent(SLUG, AgentRecord(OWNER, "eng", "t1", seat="eng-1@sw", state="finished"))
    swarm.store.put_agent(SLUG, AgentRecord(other, "eng", "t9", seat="eng-2@sw"))
    swarm.live.add(other)
    swarm.tick(START)
    swarm.tick(START + 10 * MINUTE)
    assert _reopened(swarm) and swarm.retired == []
    assert other in [a.name for a in swarm.store.agents(SLUG) if a.state != "finished"]


def test_a_task_claimed_during_the_reopen_gets_no_comment_or_alert():
    swarm = Swarm(GREEN)
    swarm.ledger.update_task = lambda slug, tid, fields, by="swarm", if_state=(): {
        "state": "open",
        "claimed_by": "engineer@next",
    }
    swarm.tick(START)
    assert swarm.tick(START + 10 * MINUTE) == []
    assert swarm.ledger.comments == [] and swarm.inbox() == []


def test_a_second_reopen_supersedes_the_open_alert():
    swarm = Swarm(GREEN)
    swarm.tick(START)
    swarm.tick(START + 10 * MINUTE)
    [first] = swarm.inbox()
    swarm.ledger.rows["t1"].update(state="pr", claimed_by=OWNER)
    [action] = swarm.tick(START + 11 * MINUTE)
    assert "for 11 minutes" in action
    closed = InboxStore(swarm.store.redis).get(first.id)
    assert (closed.state, closed.reason) == ("done", "done: superseded by a new stall alert on task t1")
    assert len(swarm.inbox()) == 2


def test_an_answered_alert_does_not_block_the_next_reopen():
    swarm = Swarm(GREEN)
    swarm.tick(START)
    swarm.tick(START + 10 * MINUTE)
    InboxStore(swarm.store.redis).close(swarm.inbox()[0].id, "master@sw", "done", "handled")
    swarm.ledger.rows["t1"].update(state="pr", claimed_by=OWNER)
    assert len(swarm.tick(START + 11 * MINUTE)) == 1 and len(swarm.inbox()) == 2


def test_an_open_unclaimed_alert_is_not_repeated_when_the_task_waits_again():
    swarm = Swarm(RUNNING)
    _full_lane(swarm)
    swarm.tick(START)
    swarm.tick(START + 15 * MINUTE)
    swarm.ready["eng"] = []
    swarm.tick(START + 16 * MINUTE)
    swarm.ready["eng"] = [swarm.ledger.rows["t1"]]
    swarm.tick(START + 17 * MINUTE)
    assert swarm.tick(START + 32 * MINUTE) == [] and len(swarm.inbox()) == 1


def test_an_unclaimed_alert_names_the_ledger_row_and_no_pull_request():
    swarm = Swarm(RUNNING)
    _full_lane(swarm)
    swarm.ledger.rows["t1"]["title"] = "Ready work"
    swarm.ready["eng"] = [{"id": "t1"}]
    swarm.tick(START)
    actions = swarm.tick(START + 15 * MINUTE)
    assert actions == [
        f"stall: task t1 ready and unclaimed while its lane is full for 15 minutes, {ALERTED}; told {LEAD}"
    ]
    [item] = swarm.inbox()
    assert item.text == _alert(**UNCLAIMED_ALERT, title="Ready work; eng lane: 1 ready tasks")


def test_an_unclaimed_task_missing_from_the_snapshot_is_named_from_its_lane():
    swarm = Swarm(RUNNING)
    _full_lane(swarm)
    swarm.ready["eng"] = [{"id": "t3", "title": "Ghost"}]
    swarm.tick(START)
    swarm.tick(START + 15 * MINUTE)
    [item] = swarm.inbox()
    assert item.text == _alert(**UNCLAIMED_ALERT, title="Ghost; eng lane: 1 ready tasks", task="t3")


def test_two_unclaimed_tasks_alert_in_the_same_tick():
    swarm = Swarm(RUNNING)
    _full_lane(swarm)
    swarm.ledger.rows["t3"] = {"id": "t3", "state": "open", "claimed_by": "", "lane": "eng"}
    swarm.ready["eng"].append(swarm.ledger.rows["t3"])
    swarm.tick(START)
    assert len(swarm.tick(START + 15 * MINUTE)) == 1


def test_many_ready_tasks_send_one_lane_alert_with_the_ready_count():
    swarm = Swarm(RUNNING)
    _full_lane(swarm)
    for index in range(3, 56):
        task = {"id": f"t{index}", "state": "open", "claimed_by": "", "lane": "eng"}
        swarm.ledger.rows[task["id"]] = task
        swarm.ready["eng"].append(task)
    swarm.tick(START)
    assert len(swarm.tick(START + 15 * MINUTE)) == 1
    [item] = swarm.inbox()
    assert "54 ready tasks" in item.text
    assert "eng lane" in item.text
    assert swarm.tick(START + 16 * MINUTE) == []


def test_an_operator_capped_lane_never_alerts_about_unclaimed_tasks():
    swarm = Swarm(RUNNING)
    _full_lane(swarm)
    swarm.store.update(SLUG, max_eng=1, max_ci=0, max_plan=0, scaling="manual")
    assert swarm.tick(START) == []
    assert swarm.tick(START + 180 * MINUTE) == []
    assert swarm.inbox() == []


def test_an_existing_lane_alert_never_escalates_after_manual_caps_apply():
    swarm = Swarm(RUNNING)
    _full_lane(swarm)
    swarm.tick(START)
    swarm.tick(START + 15 * MINUTE)
    [item] = swarm.inbox()
    swarm.store.update(SLUG, max_eng=1, max_ci=0, max_plan=0, scaling="manual")
    assert swarm.tick(START + 45 * MINUTE) == []
    assert swarm.ledger.priorities == []
    assert swarm.posted == []
    assert InboxStore(swarm.store.redis).get(item.id).state == "done"


@pytest.mark.parametrize("pull,limit", [(GREEN, 10), (CONFLICTING, 5)])
@pytest.mark.parametrize("clock", ["pushed_at", "red_at"])
def test_a_new_pull_request_event_restarts_the_stall_limit(pull, limit, clock):
    swarm = Swarm(pull, live=False)
    swarm.tick(START)
    event = START + 3 * MINUTE
    swarm.pulls[URL] = replace(pull, **{clock: event})
    assert swarm.tick(event + (limit - 1) * MINUTE) == []
    swarm.tick(event + limit * MINUTE)
    assert _reopened(swarm)


def test_another_task_record_cannot_extend_the_current_claim_limit():
    swarm = Swarm(CONFLICTING, live=False)
    swarm.store.put_agent(SLUG, AgentRecord(OWNER, "eng", "another", started_at=START + 180 * MINUTE))
    swarm.tick(START)
    swarm.tick(START + 5 * MINUTE)
    assert _reopened(swarm)


def test_a_new_push_does_not_extend_the_missing_owner_limit():
    swarm = Swarm(RUNNING, live=False)
    swarm.tick(START)
    swarm.pulls[URL] = replace(RUNNING, pushed_at=START + 9 * MINUTE)
    swarm.tick(START + 10 * MINUTE)
    assert _reopened(swarm)


def test_an_epoch_event_reopens_at_the_exact_red_limit_without_an_owner():
    swarm = Swarm(PullRequest("OPEN", None, 0, True, True, head="h"), task={"claimed_by": ""})
    swarm.tick(15 * MINUTE)
    assert _reopened(swarm)
    assert "red with no push for 15 minutes" in swarm.inbox()[0].text


@pytest.mark.parametrize("state,owner", [("claimed", ""), ("open", OWNER), ("blocked", "")])
def test_stale_ready_candidates_are_not_counted_or_alerted(state, owner):
    swarm = Swarm(RUNNING)
    _full_lane(swarm)
    swarm.ledger.rows["t1"].update(state=state, claimed_by=owner)
    assert swarm.tick(START) == []
    assert swarm.tick(START + 180 * MINUTE) == []
    assert swarm.inbox() == []


def test_a_lane_alert_without_a_snapshot_task_survives_a_ready_list_gap():
    swarm = Swarm(RUNNING)
    _full_lane(swarm)
    swarm.ready["eng"] = [{"id": "t3"}]
    swarm.tick(START)
    swarm.tick(START + 15 * MINUTE)
    [item] = swarm.inbox()
    assert "no title; eng lane: 1 ready tasks" in item.text
    assert item.state != "done"
    swarm.ready["eng"] = []
    assert swarm.tick(START + 16 * MINUTE) == []
    assert InboxStore(swarm.store.redis).get(item.id).state != "done"
    swarm.tick(START + 45 * MINUTE)
    assert [target for target, _ in swarm.ledger.priorities] == ["tasks/t3"]


def test_a_legacy_lane_alert_reads_its_lane_from_the_task():
    swarm = Swarm(RUNNING)
    _full_lane(swarm)
    swarm.tick(START)
    swarm.tick(START + 15 * MINUTE)
    [item] = swarm.inbox()
    key = swarm.store.key(SLUG, "stall-alerts")
    row = json.loads(swarm.store.redis.hget(key, "t1"))
    del row["lane"]
    swarm.store.redis.hset(key, "t1", json.dumps(row))
    assert swarm.tick(START + 16 * MINUTE) == []
    assert InboxStore(swarm.store.redis).get(item.id).state != "done"
    swarm.tick(START + 45 * MINUTE)
    assert [target for target, _ in swarm.ledger.priorities] == ["tasks/t1"]


def test_each_full_lane_alerts_once_in_the_same_tick():
    swarm = Swarm(RUNNING)
    _full_lane(swarm)
    swarm.store.put_agent(SLUG, AgentRecord("ci", "ci", "t3"))
    swarm.ledger.rows["t4"] = {"id": "t4", "state": "open", "claimed_by": "", "lane": "ci"}
    swarm.ready["ci"] = [swarm.ledger.rows["t4"]]
    swarm.tick(START)
    assert len(swarm.tick(START + 15 * MINUTE)) == 2
    items = swarm.inbox()
    assert len(items) == 2
    assert any("eng lane: 1 ready tasks" in item.text for item in items)
    assert any("ci lane: 1 ready tasks" in item.text for item in items)


def test_a_lane_alert_stays_open_when_its_first_task_is_taken():
    swarm = Swarm(RUNNING)
    _full_lane(swarm)
    swarm.ledger.rows["t3"] = {"id": "t3", "state": "open", "claimed_by": "", "lane": "eng"}
    swarm.ready["eng"].append(swarm.ledger.rows["t3"])
    swarm.tick(START)
    swarm.tick(START + 15 * MINUTE)
    [item] = swarm.inbox()
    swarm.ledger.rows["t1"].update(state="claimed", claimed_by=OWNER)
    swarm.ready["eng"] = [swarm.ledger.rows["t3"]]
    assert swarm.tick(START + 16 * MINUTE) == []
    assert InboxStore(swarm.store.redis).get(item.id).state != "done"


@pytest.mark.parametrize(
    ("lane", "configured", "agents", "alerts"),
    [
        ("eng", {"eng": 1}, [], 1),
        ("ci", {"eng": 1}, [("ci@abc-0001", "ci", "working")], 0),
        ("eng", {"eng": 2, "ci": 1}, [], 0),
        ("eng", {"eng": 0, "ci": 0}, [], 0),
        ("eng", {"eng": 1}, [(OWNER, "eng", "finished"), ("ci@abc-0001", "ci", "working")], 0),
        ("ci", {"eng": 2, "ci": 1}, [("ci@abc-0001", "ci", "working")], 1),
    ],
)
def test_a_lane_is_full_only_when_its_own_working_agents_fill_its_configured_seats(lane, configured, agents, alerts):
    swarm = Swarm(RUNNING)
    for name, agent_lane, state in agents:
        swarm.store.put_agent(SLUG, AgentRecord(name, agent_lane, "t9", state=state))
    swarm.ledger.rows["t1"].update(state="open", claimed_by="", pr_url="", lane=lane)
    swarm.ready[lane] = [swarm.ledger.rows["t1"]]
    decision = {"configured": configured, "placeable": {"claude": 1}}
    swarm.store.redis.set(swarm.store.key(SLUG, "quota-capacity"), json.dumps(decision))
    swarm.tick(START)
    swarm.tick(START + 15 * MINUTE)
    assert len(swarm.inbox()) == alerts


@pytest.mark.parametrize(("state", "claimed_by"), [("claimed", OWNER), ("claimed", ""), ("open", OWNER)])
def test_an_unclaimed_alert_closes_once_the_task_is_taken(state, claimed_by):
    swarm = Swarm(RUNNING)
    _full_lane(swarm)
    swarm.tick(START)
    swarm.tick(START + 15 * MINUTE)
    [item] = swarm.inbox()
    swarm.ledger.rows["t1"].update(state=state, claimed_by=claimed_by)
    swarm.ready["eng"] = []
    swarm.tick(START + 16 * MINUTE)
    closed = InboxStore(swarm.store.redis).get(item.id)
    assert (closed.state, closed.reason) == ("done", "done: task t1 was claimed")
    assert _alert_rows(swarm) == {}


def test_a_reopen_alert_still_escalates_after_the_next_claim():
    swarm = Swarm(GREEN)
    swarm.tick(START)
    swarm.tick(START + 10 * MINUTE)
    swarm.ledger.rows["t1"].update(state="claimed", claimed_by="engineer@abc-0009")
    swarm.live.add("engineer@abc-0009")
    swarm.pulls[URL] = RUNNING
    assert swarm.tick(START + 30 * MINUTE) == ["stall on task t1 unanswered, raised to the operator"]


def test_an_escalation_names_the_stall_in_priorities_and_ntfy():
    swarm = Swarm(GREEN)
    swarm.refusal = "ntfy failed: HTTP Error 401: Unauthorized"
    swarm.tick(START)
    swarm.tick(START + 10 * MINUTE)
    actions = swarm.tick(START + 30 * MINUTE)
    text = (
        f"Stall unanswered: task t1 green and not queued for 10 minutes, pull request {URL}, owner {OWNER}, {REOPENED}."
    )
    assert swarm.ledger.priorities == [("tasks/t1", text)]
    assert swarm.posted == [("https://ntfy.example/alerts", "Swarm sw stall", text)]
    assert swarm.envs == [swarm.environ]
    assert actions == [f"stall on task t1 unanswered, raised to the operator; {swarm.refusal}"]


def _row(tid, item, **extra):
    row = {"task": tid, "case": "green", "pr": URL, "owner": OWNER, "minutes": 10, "action": REOPENED}
    return json.dumps({**row, "limit": 10, "at": START, "item": item, **extra})


def test_an_alert_whose_item_is_gone_is_dropped():
    swarm = Swarm(RUNNING)
    swarm.store.redis.hset(swarm.store.key(SLUG, "stall-alerts"), "t1", _row("t1", "missing"))
    assert swarm.tick(START + 30 * MINUTE) == []
    assert _alert_rows(swarm) == {} and swarm.ledger.priorities == []


def test_each_alert_row_is_judged_on_its_own():
    swarm = Swarm(RUNNING)
    inbox, key = InboxStore(swarm.store.redis), swarm.store.key(SLUG, "stall-alerts")
    swarm.store.redis.hset(key, "t0", _row("t0", "missing"))
    swarm.store.redis.hset(key, "t5", _row("t5", inbox.send("swarm", "master@sw", "old").id, escalated=START))
    swarm.store.redis.hset(key, "t9", _row("t9", inbox.send("swarm", "master@sw", "due").id))
    assert swarm.tick(START + 20 * MINUTE) == ["stall on task t9 unanswered, raised to the operator"]
    assert [item for item, _ in swarm.ledger.priorities] == ["tasks/t9"]


@pytest.mark.parametrize("mode", [None, "observe", "off", "enforce"])
def test_the_watchdog_mode_controls_mutations_and_keeps_alerts(mode):
    from scripts.gates import log

    swarm = Swarm(GREEN, environ={"AGENTIHOOKS_GATE_STALL": "enforce"})
    swarm.store.update(SLUG, gates={} if mode is None else {"stall": mode})
    swarm.tick(START)
    swarm.tick(START + 10 * MINUTE)
    [item] = swarm.inbox()
    assert item.text == _alert(case="green", minutes=10, wait=20, action=REOPENED if mode == "enforce" else ALERTED)
    rows = log.recent(SLUG)
    if mode == "enforce":
        assert swarm.retired == [OWNER] and _reopened(swarm)
        assert [(row["kind"], row["tool"]) for row in rows] == [("deny", "reopen"), ("deny", "stop")]
    else:
        assert swarm.retired == [] and not _reopened(swarm)
        assert swarm.ledger.updates == [] and swarm.ledger.comments == []
        assert swarm.ledger.task(SLUG, "t1")["claimed_by"] == OWNER
        assert swarm.store.claimant(SLUG, "t1") == OWNER
        assert [agent.name for agent in swarm.store.agents(SLUG)] == [OWNER]
        expected = [] if mode == "off" else [("observe", "reopen"), ("observe", "stop")]
        assert [(row["kind"], row["tool"]) for row in rows] == expected
        if rows:
            assert all(row["gate"] == "stall" and row["agent"] == OWNER and row["task"] == "t1" for row in rows)
            assert all(row["reason"] == "green and not queued for 10 minutes" for row in rows)
            assert all(row["at"] == START + 10 * MINUTE for row in rows)
        swarm.tick(START + 30 * MINUTE)
        assert len(swarm.ledger.priorities) == 1 and len(swarm.posted) == 1


def test_five_live_watchdog_candidates_remain_claimed_for_an_hour_by_default():
    from scripts.gates import log

    swarm = Swarm(GREEN)
    swarm.store.update(SLUG, gates={})
    for index in range(2, 6):
        tid, owner = f"t{index}", f"worker-{index}"
        swarm.ledger.rows[tid] = {"id": tid, "state": "pr", "pr_url": URL, "claimed_by": owner}
        swarm.store.put_agent(SLUG, AgentRecord(owner, "eng", tid, seat=f"eng-{index}@sw"))
        swarm.store.redis.set(swarm.store.key(SLUG, "claim", tid), owner)
        swarm.live.add(owner)
    swarm.tick(START)
    for minutes in range(10, 61, 10):
        swarm.tick(START + minutes * MINUTE)
    assert swarm.retired == []
    assert swarm.ledger.updates == []
    assert all(task["state"] == "pr" for task in swarm.ledger.rows.values())
    assert len(swarm.store.agents(SLUG)) == 5
    assert all(swarm.store.claimant(SLUG, tid) == task["claimed_by"] for tid, task in swarm.ledger.rows.items())
    rows = log.recent(SLUG, limit=None)
    assert len(rows) == 60
    assert sum(row["tool"] == "stop" for row in rows) == 30
    assert sum(row["tool"] == "reopen" for row in rows) == 30
    assert all(row["kind"] == "observe" for row in rows)
    assert len(swarm.inbox()) == 5


@pytest.mark.parametrize("owner", ["", None])
def test_an_ownerless_stall_logs_only_a_reopen_without_retiring_another_agent(owner):
    from scripts.gates import log

    swarm = Swarm(RED, task={"claimed_by": owner})
    swarm.store.update(SLUG, gates={})
    swarm.tick(START)
    swarm.tick(START + 15 * MINUTE)
    assert swarm.retired == [] and swarm.ledger.updates == []
    [row] = log.recent(SLUG)
    assert (row["gate"], row["kind"], row["agent"], row["task"], row["tool"]) == (
        "stall",
        "observe",
        "",
        "t1",
        "reopen",
    )
    assert [agent.name for agent in swarm.store.agents(SLUG)] == [OWNER]


@pytest.mark.parametrize("first, second", [("enforce", "observe"), ("observe", "enforce")])
def test_a_mode_change_between_logged_actions_never_retires_or_reopens(first, second, monkeypatch):
    from scripts.gates import log

    swarm = Swarm(GREEN)
    swarm.store.update(SLUG, gates={"stall": first})
    append = log.append

    def change_mode(slug, row, home=None):
        assert slug == SLUG
        append(slug, row, home)
        if row.tool == "reopen":
            swarm.store.update(SLUG, gates={"stall": second})

    monkeypatch.setattr(log, "append", change_mode)
    swarm.tick(START)
    swarm.tick(START + 10 * MINUTE)
    assert swarm.retired == [] and swarm.ledger.updates == []
    assert swarm.store.claimant(SLUG, "t1") == OWNER
    assert [(row["kind"], row["tool"]) for row in log.recent(SLUG)] == [
        ("deny" if first == "enforce" else "observe", "reopen"),
        ("deny" if second == "enforce" else "observe", "stop"),
    ]


RULES = [(GREEN, True, 10), (RED, True, 15), (RUNNING, False, 10), (CONFLICTING, False, 5)]


def _wait(swarm, on, name=OWNER):
    idle.declare_wait(swarm.store.redis, SLUG, name, START + 600 * MINUTE, "held behind its fix", START, on=on)


def _held_then_released(swarm, limit, release):
    assert swarm.tick(START) == []
    assert swarm.tick(START + 6 * limit * MINUTE) == []
    assert not _reopened(swarm) and swarm.retired == [] and swarm.inbox() == [] and swarm.ledger.updates == []
    release()
    swarm.tick(START + 7 * limit * MINUTE)
    swarm.tick(START + 8 * limit * MINUTE)
    assert _reopened(swarm)


@pytest.mark.parametrize(("pull", "live", "limit"), RULES)
def test_an_open_dependency_holds_every_stall_rule_until_it_is_done(pull, live, limit):
    swarm = Swarm(pull, task={"depends_on": ["fix"]}, live=live)
    swarm.ledger.rows["fix"] = {"id": "fix", "state": "pr"}
    _held_then_released(swarm, limit, lambda: swarm.ledger.rows["fix"].update(state="done"))


@pytest.mark.parametrize("depends_on", [["t1"], []])
def test_an_own_or_no_dependency_does_not_hold_a_stall(depends_on):
    swarm = Swarm(RED, task={"depends_on": depends_on})
    swarm.tick(START + 15 * MINUTE)
    assert _reopened(swarm)


@pytest.mark.parametrize("state", ["blocked", "claimed", None])
def test_a_blocked_or_unknown_dependency_holds_a_stall_as_claiming_does(state):
    swarm = Swarm(RED, task={"depends_on": ["fix"]})
    if state:
        swarm.ledger.rows["fix"] = {"id": "fix", "state": state}
    assert swarm.tick(START + 60 * MINUTE) == []
    assert swarm.ledger.rows["t1"]["state"] == "pr" and swarm.retired == []


def test_a_release_record_without_awaiting_does_not_hold_a_stall():
    swarm = Swarm(RED)
    swarm.ledger.rows["repair"] = {"id": "repair", "state": "pr"}
    found = ready.held_record(URL, "h", "", OWNER, START)
    found["dependency"] = {"kind": "task", "target": "repair"}
    ready.save(swarm.store, SLUG, "t1", found)
    swarm.tick(START + 15 * MINUTE)
    assert _reopened(swarm)


@pytest.mark.parametrize(("pull", "live", "limit"), RULES)
def test_an_owner_wait_on_an_open_task_holds_every_stall_rule_until_it_is_done(pull, live, limit):
    swarm = Swarm(pull, live=live)
    swarm.ledger.rows["fix"] = {"id": "fix", "state": "claimed"}
    _wait(swarm, {"kind": "task", "target": "fix"})
    _held_then_released(swarm, limit, lambda: swarm.ledger.rows["fix"].update(state="done"))


@pytest.mark.parametrize(("pull", "live", "limit"), RULES)
def test_an_owner_wait_on_another_open_pull_request_holds_every_stall_rule_until_it_merges(pull, live, limit):
    swarm = Swarm(pull, live=live)
    other = URL + "1"
    swarm.pulls[other] = RUNNING
    _wait(swarm, {"kind": "merge", "target": other})
    _held_then_released(swarm, limit, lambda: swarm.pulls.update({other: replace(RUNNING, state="MERGED")}))


@pytest.mark.parametrize(
    ("name", "on"),
    [
        (OWNER, {"kind": "checks", "target": URL}),
        (OWNER, {"kind": "merge", "target": URL}),
        (OWNER, None),
        ("engineer@abc-0002", {"kind": "task", "target": "fix"}),
    ],
)
def test_a_wait_on_its_own_pull_request_or_by_another_agent_does_not_hold_a_stall(name, on):
    swarm = Swarm(RED)
    swarm.ledger.rows["fix"] = {"id": "fix", "state": "claimed"}
    _wait(swarm, on, name)
    swarm.tick(START + 15 * MINUTE)
    assert _reopened(swarm)


@pytest.mark.parametrize(("pull", "limit"), [(GREEN, 10), (RED, 15)])
def test_a_wait_the_tick_ended_gives_its_owner_the_full_limit_from_the_wake(pull, limit):
    swarm = Swarm(pull)
    inbox = InboxStore(swarm.store.redis)
    asked = inbox.send("master@sw", OWNER, "review this").id
    _wait(swarm, {"kind": "reply", "target": asked})
    assert swarm.tick(START) == []
    woke = START + 2 * limit * MINUTE
    inbox.close(asked, OWNER, "done", "answered")
    rows = {t["id"]: t for t in swarm.ledger.state(SLUG)["tasks"]}
    assert waits.end_pass(swarm.store, SLUG, rows, inbox, swarm.pulls.get, woke, swarm.pulls.get)
    assert 0 < swarm.store.redis.ttl(idle.key(SLUG, "woke", OWNER)) <= idle.BEAT_TTL_S
    idle.declare_wait(
        swarm.store.redis, SLUG, OWNER, woke + 600 * MINUTE, "queued", woke, on={"kind": "merge", "target": URL}
    )
    assert swarm.tick(woke) == [] and swarm.tick(woke + (limit - 1) * MINUTE) == []
    assert not _reopened(swarm) and swarm.retired == []
    swarm.tick(woke + limit * MINUTE)
    assert _reopened(swarm)


def test_a_wait_the_owner_ends_itself_moves_no_stall_clock():
    swarm = Swarm(GREEN)
    assert swarm.tick(START) == []
    idle.end_wait(swarm.store.redis, SLUG, OWNER, START + 9 * MINUTE)
    assert idle.woke(swarm.store.redis, SLUG, OWNER) == 0
    swarm.tick(START + 10 * MINUTE)
    assert _reopened(swarm)


@pytest.mark.parametrize(("pull", "live", "limit"), RULES)
def test_a_release_hold_keeps_every_stall_rule_until_a_newer_release_exists(pull, live, limit):
    swarm = Swarm(pull, task={release_wait.FIELD: "v2.17.3"}, live=live)
    tags = ["v2.17.3"]

    def tick(at):
        return swarm.tick(at, released=lambda task: release_wait.newer(tags, task[release_wait.FIELD]))

    assert tick(START) == [] and tick(START + 6 * limit * MINUTE) == []
    assert not _reopened(swarm) and swarm.retired == [] and swarm.inbox() == []
    tags.append("v2.17.4")
    tick(START + 7 * limit * MINUTE)
    tick(START + 8 * limit * MINUTE)
    assert _reopened(swarm)


REFRESHING = replace(RUNNING, pushed_at=START + MINUTE, checks_pending=True, required_pending=True)
RESTARTED_AT = "1970-01-01T02:50:00Z"


def _idle_owner(swarm):
    swarm.store.put_agent(SLUG, AgentRecord(OWNER, "eng", "t1", state="idle", seat="eng-1@sw"))


def test_a_dropped_pull_request_with_refreshed_checks_in_progress_waits_for_them():
    swarm = Swarm(QUEUED)
    _idle_owner(swarm)
    swarm.tick(START)
    swarm.pulls[URL] = REFRESHING
    for minute in (1, 6, 30):
        assert swarm.tick(START + minute * MINUTE) == []
    assert not _reopened(swarm) and swarm.retired == [] and swarm.inbox() == []


def test_a_pull_request_removed_from_the_queue_and_not_requeued_is_still_reported():
    swarm = Swarm(QUEUED)
    _idle_owner(swarm)
    swarm.tick(START)
    swarm.pulls[URL] = GREEN
    swarm.tick(START + MINUTE)
    assert not _reopened(swarm)
    swarm.tick(START + 6 * MINUTE)
    assert _reopened(swarm) and swarm.retired == [OWNER]
    assert "unmergeable or dropped from the queue for 5 minutes" in swarm.inbox()[0].text


def test_refreshed_checks_never_hold_a_dropped_pull_request_whose_owner_is_gone():
    swarm = Swarm(QUEUED, live=False)
    swarm.tick(START)
    swarm.pulls[URL] = REFRESHING
    swarm.tick(START + MINUTE)
    swarm.tick(START + 6 * MINUTE)
    assert not _reopened(swarm)
    swarm.tick(START + 10 * MINUTE)
    assert _reopened(swarm)
    assert "owner not live for 10 minutes" in swarm.inbox()[0].text


@pytest.mark.parametrize("fresh", [QUEUED, None, replace(GREEN, state="MERGED"), REFRESHING])
def test_a_stall_the_decision_time_read_no_longer_shows_keeps_its_owner(fresh):
    swarm = Swarm(QUEUED)
    _idle_owner(swarm)
    swarm.tick(START)
    swarm.pulls[URL] = GREEN
    swarm.tick(START + MINUTE, reread={URL: fresh}.get)
    assert swarm.tick(START + 6 * MINUTE, reread={URL: fresh}.get) == []
    assert not _reopened(swarm) and swarm.retired == [] and swarm.inbox() == []


def test_a_stall_the_decision_time_read_confirms_reopens_the_task():
    swarm = Swarm(QUEUED)
    _idle_owner(swarm)
    swarm.tick(START)
    swarm.pulls[URL] = REFRESHING
    swarm.tick(START + MINUTE, reread={URL: GREEN}.get)
    assert not _reopened(swarm)
    swarm.pulls[URL] = GREEN
    swarm.tick(START + 2 * MINUTE, reread={URL: GREEN}.get)
    swarm.tick(START + 7 * MINUTE, reread={URL: GREEN}.get)
    assert _reopened(swarm) and swarm.retired == [OWNER]


def _restarted_red(started, restarted=RESTARTED_AT):
    return ledger_events.pull_request(
        {
            "state": "OPEN",
            "timelineItems": {"nodes": [{"createdAt": restarted}] if restarted else []},
            "statusCheckRollup": [
                {"name": "Tests", "conclusion": "FAILURE", "startedAt": started, "completedAt": "1970-01-01T02:55:00Z"}
            ],
            "checkSuites": [
                {"status": "COMPLETED", "conclusion": "FAILURE", "workflowRun": {"databaseId": 1, "createdAt": started}}
            ],
        }
    )


@pytest.mark.parametrize(
    "started,restarted,reopens",
    [
        ("1970-01-01T02:45:00Z", RESTARTED_AT, False),
        ("1970-01-01T02:51:00Z", RESTARTED_AT, True),
        ("1970-01-01T02:50:00Z", RESTARTED_AT, True),
        ("1970-01-01T02:45:00Z", None, True),
    ],
)
def test_only_red_runs_started_after_the_latest_reopen_or_queue_entry_stop_an_owner(started, restarted, reopens):
    pull = _restarted_red(started, restarted)
    assert pull.red and pull.stale_red == (not reopens)
    swarm = Swarm(pull)
    swarm.tick(START + 20 * MINUTE)
    swarm.tick(START + 40 * MINUTE)
    assert _reopened(swarm) == reopens
    assert swarm.retired == ([OWNER] if reopens else [])


def test_a_red_check_without_a_start_time_is_never_stale():
    raw = {
        "state": "OPEN",
        "timelineItems": {"nodes": [None, {"createdAt": RESTARTED_AT}, {"createdAt": "1970-01-01T02:40:00Z"}]},
        "statusCheckRollup": [{"name": "Tests", "conclusion": "FAILURE", "completedAt": "1970-01-01T02:55:00Z"}],
    }
    assert ledger_events.pull_request(raw).stale_red is False
    status = {"context": "sonar", "state": "FAILURE", "createdAt": "1970-01-01T02:45:00Z"}
    assert ledger_events.pull_request({**raw, "statusCheckRollup": [status]}).stale_red is True
    assert ledger_events.pull_request({"state": "OPEN", "statusCheckRollup": [status]}).stale_red is False


def test_a_stale_red_run_beside_fresh_passing_runs_is_stale_and_pending_required_checks_are_read():
    fresh = {"name": "Lint", "conclusion": "SUCCESS", "startedAt": "1970-01-01T02:51:00Z"}
    stale = {"name": "Tests", "conclusion": "FAILURE", "startedAt": "1970-01-01T02:45:00Z"}
    running = {"name": "Build", "conclusion": None, "startedAt": "1970-01-01T02:52:00Z"}
    raw = {"state": "OPEN", "timelineItems": {"nodes": [{"createdAt": RESTARTED_AT}]}}
    pull = ledger_events.pull_request({**raw, "statusCheckRollup": [stale, fresh]})
    assert pull.red and pull.stale_red and not pull.required_pending
    optional = {**running, "isRequired": False}
    assert ledger_events.pull_request({**raw, "statusCheckRollup": [fresh, running]}).required_pending is True
    optional_pull = ledger_events.pull_request({**raw, "statusCheckRollup": [{**fresh, "isRequired": True}, optional]})
    assert optional_pull.checks_pending and not optional_pull.required_pending
    expected = {"context": "deleted workflow", "state": "EXPECTED", "createdAt": "1970-01-01T02:52:00Z"}
    assert ledger_events.pull_request({**raw, "statusCheckRollup": [fresh, expected]}).required_pending is False


def test_only_pending_optional_checks_never_hold_a_dropped_pull_request():
    swarm = Swarm(QUEUED)
    _idle_owner(swarm)
    swarm.tick(START)
    swarm.pulls[URL] = replace(GREEN, checks_pending=True)
    swarm.tick(START + MINUTE)
    swarm.tick(START + 6 * MINUTE)
    assert _reopened(swarm) and swarm.retired == [OWNER]


def test_a_push_seen_only_by_the_decision_time_read_restarts_the_red_limit():
    swarm = Swarm(RED)
    pushed = replace(RED, pushed_at=START + 19 * MINUTE)
    assert swarm.tick(START + 20 * MINUTE, reread={URL: pushed}.get) == []
    assert not _reopened(swarm) and swarm.retired == []
    swarm.tick(START + 34 * MINUTE, reread={URL: pushed}.get)
    assert _reopened(swarm)


def test_the_decision_time_read_keeps_a_working_owner_on_a_conflict():
    swarm = Swarm(GREEN)
    _idle_owner(swarm)
    swarm.tick(START)
    assert swarm.tick(START + 10 * MINUTE, reread={URL: CONFLICTING}.get) == []
    swarm.store.put_agent(SLUG, AgentRecord(OWNER, "eng", "t1", state="working", seat="eng-1@sw"))
    assert swarm.tick(START + 16 * MINUTE, reread={URL: CONFLICTING}.get) == []
    assert not _reopened(swarm) and swarm.retired == []


def test_the_pull_request_view_reads_check_starts_and_the_latest_reopen_or_queue_entry():
    assert "CheckRun{databaseId name conclusion startedAt completedAt" in ledger_events.CONTEXT_NODES
    assert (
        "timelineItems(last:1,itemTypes:[REOPENED_EVENT,ADDED_TO_MERGE_QUEUE_EVENT])"
        "{nodes{...on ReopenedEvent{createdAt} ...on AddedToMergeQueueEvent{createdAt}}}"
    ) in ledger_events.PULL_QUERY


def test_the_tick_rereads_a_stall_from_github_before_acting():
    import inspect

    from scripts.swarm import cli

    assert "reread=ledger_events.view" in inspect.getsource(cli.run_tick)


def test_a_new_owner_inside_its_limit_costs_no_github_reread():
    swarm = Swarm(RED)
    swarm.tick(START)
    claim = START + 180 * MINUTE
    swarm.store.put_agent(SLUG, AgentRecord(OWNER, "eng", "t1", started_at=claim, seat="eng-1@sw"))
    reads = []
    assert swarm.tick(claim + MINUTE, reread=lambda url: reads.append(url) or RED) == []
    assert reads == []
