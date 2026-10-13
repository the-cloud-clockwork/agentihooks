import copy
import json

import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm import ledger_events, ready, stall_watch
from scripts.swarm.ledger_events import PullRequest
from scripts.swarm.store import AgentRecord, RedisStore
from tests.swarm.test_tick import FakeLedger

pytestmark = pytest.mark.xdist_group("fakeredis")

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


class Ledger(FakeLedger):
    def __init__(self, tasks):
        super().__init__(tasks)
        self.priorities = []

    def comment(self, slug, task, text, by):
        self.comments.append((task, text, by))

    def priority(self, slug, item, text):
        self.priorities.append((item, text))


class Swarm:
    def __init__(self, pull=GREEN, task=None, live=True, environ=None):
        import fakeredis

        self.store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
        self.ledger = Ledger(
            [{"id": "t1", "state": "pr", "pr_url": URL, "claimed_by": OWNER, "branch": "b1", **(task or {})}]
        )
        self.pulls = {URL: pull}
        self.retired, self.posted, self.ready = [], [], {"eng": [], "ci": []}
        self.live = {OWNER} if live else set()
        self.environ = {stall_watch.NTFY: "https://ntfy.example/alerts", **(environ or {})}
        self.store.put_agent(SLUG, AgentRecord(OWNER, "eng", "t1", seat="eng-1@sw"))
        self.store.redis.set(self.store.key(SLUG, "claim", "t1"), OWNER)

    def tick(self, at, doc=None):
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
            lambda url, title, text: self.posted.append((url, title, text)) or "",
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


def test_an_unmergeable_pull_request_reopens_after_five_minutes():
    swarm = Swarm(CONFLICTING)
    swarm.tick(START)
    assert not _reopened(swarm)
    swarm.tick(START + 5 * MINUTE)
    assert _reopened(swarm)
    assert "unmergeable or dropped from the queue for 5 minutes" in swarm.inbox()[0].text


def test_a_pull_request_dropped_from_the_queue_reopens_after_five_minutes():
    swarm = Swarm(QUEUED)
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
    [found] = stall_watch.findings(swarm.store, SLUG)
    assert found.kind == "stall" and found.subject == "t1" and found.measure == 10
    assert any(URL in line for line in found.evidence) and any(OWNER in line for line in found.evidence)
    InboxStore(swarm.store.redis).close(swarm.inbox()[0].id, "master@sw", "done", "handled")
    swarm.tick(START + 30 * MINUTE)
    assert swarm.ledger.priorities == [] and stall_watch.findings(swarm.store, SLUG) == []


def test_without_an_ntfy_setting_escalation_still_reaches_priorities():
    swarm = Swarm(GREEN)
    del swarm.environ[stall_watch.NTFY]
    swarm.tick(START)
    swarm.tick(START + 10 * MINUTE)
    actions = swarm.tick(START + 30 * MINUTE)
    assert len(swarm.ledger.priorities) == 1 and swarm.posted == []
    assert any("ntfy is not configured" in a for a in actions)


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


def test_an_outage_holds_the_reopen(monkeypatch):
    monkeypatch.setattr(stall_watch.outage, "held", lambda store, slug, name: "ledger health probe failed")
    swarm = Swarm(GREEN)
    swarm.tick(START)
    assert swarm.tick(START + 30 * MINUTE) == [] and not _reopened(swarm) and swarm.retired == []


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
