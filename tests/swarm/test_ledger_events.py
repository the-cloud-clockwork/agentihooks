import json
from pathlib import Path

import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm import ledger_events
from scripts.swarm.store import MASTER, AgentRecord, RedisStore, SwarmConfig

pytestmark = pytest.mark.xdist_group("fakeredis")

MINUTE = 60_000
ANSWERS = Path(__file__).resolve().parents[1] / "fixtures" / "github_pr"
MASTER_SEAT, ENG_SEAT = "master@sw", "eng-1@sw"


class FakeLedger:
    def __init__(self):
        self.priorities = []

    def priority(self, slug, item, text):
        self.priorities.append((item, text))


@pytest.fixture
def store():
    import fakeredis

    s = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    s.create(SwarmConfig("sw", "/repo", max_eng=2, max_ci=1))
    s.put_agent("sw", AgentRecord("sw-master-1", MASTER, MASTER, seat=MASTER_SEAT))
    s.put_agent("sw", AgentRecord("sw-eng-1", "eng", "t1", seat=ENG_SEAT))
    return s


def answer(name):
    return ledger_events.pull_request(json.loads((ANSWERS / f"{name}.json").read_text()))


def event(rev, at, by, kind, target, text=""):
    return {"rev": rev, "at": at, "by": by, "kind": kind, "target": target, **({"text": text} if text else {})}


def recorded(extra_events=(), rev=10, followups=(), questions=(), tasks=()):
    base = [
        event(2, 1_000, "init-swarm", "added", "tasks/t1", "Build the thing"),
        event(3, 2_000, "sw-eng-1", "joined", ""),
        event(4, 3_000, "sw-eng-1", "comment added", "phases/p1", "Started"),
    ]
    events = base + list(extra_events)
    return {
        "_meta": {"rev": max([rev] + [e["rev"] for e in events]), "events": events},
        "tasks": list(tasks)
        or [{"id": "t1", "title": "Build the thing", "state": "claimed", "claimed_by": "sw-eng-1"}],
        "followups": list(followups),
        "questions": list(questions),
    }


AGENT_EVENTS = [
    event(11, 100_000, "sw-eng-1", "added", "followups/f1", "Retries need a cap"),
    event(12, 100_000, "sw-eng-1", "added", "questions/q1", "Which port should the service use"),
    event(13, 100_000, "sw-eng-1", "task blocked", "tasks/t1"),
    event(14, 100_000, "sw-eng-2", "task done", "tasks/t2"),
]
NOISE = [
    event(15, 100_000, "operator", "added", "followups/f2", "Operator wrote this"),
    event(16, 100_000, "sw-master-1", "added", "followups/f3", "Master wrote this"),
    event(17, 100_000, "sw-eng-1", "comment added", "tasks/t1", "progress"),
    event(18, 100_000, "sw-eng-1", "task pr", "tasks/t1"),
]
DONE_T2 = {"id": "t2", "title": "Ship the other thing", "state": "done", "pr_url": "https://github.com/o/r/pull/7"}


def run(store, doc, now_ms=100_000, ledger=None, github=lambda url: None):
    return ledger_events.event_pass(InboxStore(store.redis), store, "sw", doc, ledger or FakeLedger(), now_ms, github)


def texts(store, address):
    return [i.text for i in InboxStore(store.redis).inbox(address)]


def test_first_pass_starts_at_the_ledger_revision_and_sends_nothing(store):
    run(store, recorded(AGENT_EVENTS))
    assert texts(store, MASTER_SEAT) == []


def test_each_agent_event_kind_makes_exactly_one_master_item(store):
    run(store, recorded())
    tasks = [{"id": "t1", "title": "Build the thing", "state": "blocked", "claimed_by": "sw-eng-1"}, DONE_T2]
    run(store, recorded(AGENT_EVENTS + NOISE, tasks=tasks))
    followup, question, blocked, done = texts(store, MASTER_SEAT)
    assert "Retries need a cap" in followup and "sw-eng-1" in followup
    assert "Which port should the service use" in question
    assert "Build the thing" in blocked
    assert "Ship the other thing" in done and "proof" in done and "https://github.com/o/r/pull/7" in done
    assert texts(store, ENG_SEAT) == []


def test_a_replay_of_the_same_ledger_makes_no_item(store):
    run(store, recorded())
    doc = recorded(AGENT_EVENTS, tasks=[DONE_T2])
    run(store, doc)
    run(store, doc)
    assert len(texts(store, MASTER_SEAT)) == 4


def test_the_cursor_is_kept_per_swarm(store):
    store.create(SwarmConfig("other", "/repo", max_eng=1, max_ci=0))
    run(store, recorded())
    ledger_events.event_pass(InboxStore(store.redis), store, "other", recorded(), FakeLedger(), 100_000, lambda u: None)
    run(store, recorded(AGENT_EVENTS[:1]))
    assert len(texts(store, MASTER_SEAT)) == 1


def test_items_go_to_the_master_seat_when_no_master_is_live(store):
    store.drop_agent("sw", "sw-master-1")
    run(store, recorded())
    run(store, recorded(AGENT_EVENTS[:1]))
    assert len(texts(store, MASTER_SEAT)) == 1


def open_followup(added_at, **fields):
    doc = recorded(
        [event(11, added_at, "sw-eng-1", "added", "followups/f1", "Retries need a cap")],
        followups=[{"id": "f1", "text": "Retries need a cap", "comments": [], "done": False, **fields}],
    )
    return doc


def test_an_open_follow_up_is_raised_again_after_fifteen_minutes_then_shown_to_the_operator(store):
    ledger = FakeLedger()
    run(store, recorded())
    doc = open_followup(0)
    run(store, doc, now_ms=1, ledger=ledger)
    run(store, doc, now_ms=15 * MINUTE - 1, ledger=ledger)
    assert len(texts(store, MASTER_SEAT)) == 1
    run(store, doc, now_ms=15 * MINUTE, ledger=ledger)
    run(store, doc, now_ms=16 * MINUTE, ledger=ledger)
    sent = texts(store, MASTER_SEAT)
    assert len(sent) == 2 and "fifteen minutes" in sent[1] and "Retries need a cap" in sent[1]
    assert ledger.priorities == []
    run(store, doc, now_ms=30 * MINUTE, ledger=ledger)
    run(store, doc, now_ms=31 * MINUTE, ledger=ledger)
    assert [item for item, _ in ledger.priorities] == ["followups/f1"]
    assert "Retries need a cap" in ledger.priorities[0][1]


def test_a_closed_or_operator_flagged_follow_up_is_not_raised(store):
    ledger = FakeLedger()
    run(store, recorded())
    for fields in ({"done": True}, {"needs_operator": True}):
        run(store, open_followup(0, **fields), now_ms=40 * MINUTE, ledger=ledger)
    assert len(texts(store, MASTER_SEAT)) == 1
    assert ledger.priorities == []


def in_pr(url="https://github.com/o/r/pull/9"):
    return recorded(
        tasks=[{"id": "t1", "title": "Build the thing", "state": "pr", "claimed_by": "sw-eng-1", "pr_url": url}]
    )


def test_recorded_answers_parse():
    merged, closed, red = answer("merged"), answer("closed_unmerged"), answer("open_red")
    assert (merged.state, merged.red) == ("MERGED", False)
    assert merged.merged_at == ledger_events.iso_ms("2026-10-05T17:34:59Z")
    assert (closed.state, closed.merged_at) == ("CLOSED", None)
    assert (red.state, red.red) == ("OPEN", True)
    assert red.pushed_at == ledger_events.iso_ms("2026-10-05T01:40:12Z")


def test_a_task_left_in_pr_after_its_merge_goes_to_its_engineer_then_the_master(store):
    merged = answer("merged")
    run(store, recorded())
    for minutes in (9, 10, 11):
        run(store, in_pr(), now_ms=merged.merged_at + minutes * MINUTE, github=lambda url: merged)
    assert len(texts(store, ENG_SEAT)) == 1 and "merged" in texts(store, ENG_SEAT)[0]
    assert texts(store, MASTER_SEAT) == []
    for minutes in (20, 21):
        run(store, in_pr(), now_ms=merged.merged_at + minutes * MINUTE, github=lambda url: merged)
    assert len(texts(store, MASTER_SEAT)) == 1 and "Build the thing" in texts(store, MASTER_SEAT)[0]
    assert len(texts(store, ENG_SEAT)) == 1


def test_a_pull_request_closed_unmerged_goes_to_its_engineer_once(store):
    closed = answer("closed_unmerged")
    run(store, recorded())
    run(store, in_pr(), github=lambda url: closed)
    run(store, in_pr(), github=lambda url: closed)
    assert len(texts(store, ENG_SEAT)) == 1 and "closed" in texts(store, ENG_SEAT)[0]


def test_red_checks_with_no_push_for_twenty_minutes_go_to_its_engineer(store):
    red = answer("open_red")
    run(store, recorded())
    run(store, in_pr(), now_ms=red.pushed_at + 19 * MINUTE, github=lambda url: red)
    assert texts(store, ENG_SEAT) == []
    run(store, in_pr(), now_ms=red.pushed_at + 20 * MINUTE, github=lambda url: red)
    run(store, in_pr(), now_ms=red.pushed_at + 21 * MINUTE, github=lambda url: red)
    assert len(texts(store, ENG_SEAT)) == 1 and "red" in texts(store, ENG_SEAT)[0]
    pushed = ledger_events.PullRequest("OPEN", None, red.pushed_at + 30 * MINUTE, True)
    run(store, in_pr(), now_ms=pushed.pushed_at + 20 * MINUTE, github=lambda url: pushed)
    assert len(texts(store, ENG_SEAT)) == 2


def test_green_or_unreadable_pull_requests_make_no_item(store):
    green = answer("merged")
    run(store, recorded())
    run(store, in_pr(), now_ms=green.merged_at + MINUTE, github=lambda url: green)
    run(store, in_pr(), now_ms=green.merged_at + 60 * MINUTE, github=lambda url: None)
    assert texts(store, ENG_SEAT) == [] and texts(store, MASTER_SEAT) == []


def test_a_gone_engineer_falls_back_to_the_master(store):
    closed = answer("closed_unmerged")
    store.drop_agent("sw", "sw-eng-1")
    run(store, recorded())
    run(store, in_pr(), github=lambda url: closed)
    assert len(texts(store, MASTER_SEAT)) == 1


def finding(fid, verdict=None):
    kind, subject = fid.split("/")
    return {"id": fid, "kind": kind, "subject": subject, "summary": f"{kind} on {subject}", "verdict": verdict}


def test_every_new_health_finding_goes_to_the_master_once(store):
    inbox = InboxStore(store.redis)
    shown = [finding("stale-claim/t1"), finding("proof-loop/t2")]
    ledger_events.findings_pass(inbox, store, "sw", shown)
    ledger_events.findings_pass(inbox, store, "sw", shown)
    sent = texts(store, MASTER_SEAT)
    assert len(sent) == 2
    assert "agentihooks swarm sw verdict stale-claim/t1" in sent[0]
    returned = [finding("stale-claim/t1", {"value": "early-real", "at": 5})]
    ledger_events.findings_pass(inbox, store, "sw", returned)
    assert len(texts(store, MASTER_SEAT)) == 3
