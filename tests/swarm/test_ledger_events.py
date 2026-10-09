import dataclasses
import json
from pathlib import Path

import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm import ledger_events
from scripts.swarm.store import MASTER, AgentRecord, RedisStore, SwarmConfig

pytestmark = pytest.mark.xdist_group("fakeredis")

MINUTE = 60_000
ANSWERS = Path(__file__).resolve().parents[1] / "fixtures" / "github_pr"
GATE = "Gate — Required"
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


def test_only_the_task_done_notice_is_informational(store):
    run(store, recorded())
    tasks = [{"id": "t1", "title": "Build the thing", "state": "blocked", "claimed_by": "sw-eng-1"}, DONE_T2]
    run(store, recorded(AGENT_EVENTS, tasks=tasks))
    assert [i.fyi for i in InboxStore(store.redis).inbox(MASTER_SEAT)] == [False, False, False, True]


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


def test_the_event_cursor_is_stored_under_its_own_name(store):
    ledger_events.event_pass(
        InboxStore(store.redis), store, "sw", recorded(rev=7), FakeLedger(), 0, github=lambda u: None
    )
    assert store.redis.get(store.key("sw", "events-cursor")) == "7"
    assert ledger_events.new_events(store, "sw", recorded(rev=7), "other-cursor") == []
    assert store.redis.get(store.key("sw", "other-cursor")) == "7"


def test_items_go_to_the_master_seat_when_no_master_is_live(store):
    store.drop_agent("sw", "sw-master-1")
    run(store, recorded())
    run(store, recorded(AGENT_EVENTS[:1]))
    assert len(texts(store, MASTER_SEAT)) == 1


def priority(item, text, by="sw-eng-1", pid=""):
    return {"id": pid or f"p-{item}", "item": item, "text": text, "by": by, "at": 1_000}


def with_priorities(*rows):
    doc = recorded()
    doc["priorities"] = list(rows)
    return doc


def test_a_new_priority_makes_one_master_item_naming_the_item_and_the_ask(store):
    run(store, with_priorities())
    run(store, with_priorities(priority("tasks/t1", "Blocked: the deploy key is missing", by="ledger")))
    run(store, with_priorities(priority("tasks/t1", "Blocked: the deploy key is missing", by="ledger")))
    (item,) = InboxStore(store.redis).inbox(MASTER_SEAT)
    assert "tasks/t1" in item.text and "Blocked: the deploy key is missing" in item.text
    assert "triage" in item.text.lower() and not item.fyi
    assert texts(store, ENG_SEAT) == []


def test_priorities_open_before_the_first_pass_make_no_item(store):
    run(store, with_priorities(priority("questions/q1", "Answer: which port")))
    run(store, with_priorities(priority("questions/q1", "Answer: which port")))
    assert texts(store, MASTER_SEAT) == []


def test_a_repeated_ask_on_the_same_item_makes_nothing_new(store):
    run(store, with_priorities())
    run(store, with_priorities(priority("followups/f1", "Decide: retries need a cap")))
    run(store, with_priorities(priority("followups/f1", "Decide: retries need a cap now", pid="p-again")))
    assert len(texts(store, MASTER_SEAT)) == 1


def test_a_priority_raised_again_after_it_cleared_makes_a_new_item(store):
    row = priority("tasks/t1", "Blocked: the deploy key is missing", by="ledger")
    run(store, with_priorities())
    run(store, with_priorities(row))
    run(store, with_priorities())
    run(store, with_priorities(row))
    assert len(texts(store, MASTER_SEAT)) == 2


def test_a_priority_the_master_added_makes_no_item(store):
    run(store, with_priorities())
    run(store, with_priorities(priority("phases/p1", "Approve the plan", by="sw-master-1")))
    assert texts(store, MASTER_SEAT) == []


def test_a_swarm_with_no_live_master_gets_no_priority_item(store):
    store.drop_agent("sw", "sw-master-1")
    run(store, with_priorities())
    run(store, with_priorities(priority("tasks/t1", "Blocked: the deploy key is missing", by="ledger")))
    assert texts(store, MASTER_SEAT) == []


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
    assert red.pushed_at == ledger_events.iso_ms("2026-10-05T01:40:19Z")
    assert red.red_at == ledger_events.iso_ms("2026-10-05T01:40:36Z")


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
    run(store, in_pr(), now_ms=red.red_at + 19 * MINUTE, github=lambda url: red)
    assert texts(store, ENG_SEAT) == []
    run(store, in_pr(), now_ms=red.red_at + 20 * MINUTE, github=lambda url: red)
    run(store, in_pr(), now_ms=red.red_at + 21 * MINUTE, github=lambda url: red)
    assert len(texts(store, ENG_SEAT)) == 1 and "red" in texts(store, ENG_SEAT)[0]
    rerun = dataclasses.replace(red, red_at=red.red_at + 10 * MINUTE)
    run(store, in_pr(), now_ms=rerun.red_at + 20 * MINUTE, github=lambda url: rerun)
    assert len(texts(store, ENG_SEAT)) == 1
    pushed = ledger_events.PullRequest("OPEN", None, red.pushed_at + 30 * MINUTE, True, head="pushed")
    told = run(store, in_pr(), now_ms=pushed.pushed_at + 20 * MINUTE, github=lambda url: pushed)
    assert f"told {ENG_SEAT}: https://github.com/o/r/pull/9:red:pushed" in told
    assert len(texts(store, ENG_SEAT)) == 2


@pytest.mark.parametrize(("gate", "notices"), [("SUCCESS", 0), ("FAILURE", 1)])
def test_a_red_job_beside_the_required_gate_is_noticed_only_when_the_gate_failed(store, gate, notices):
    rollup = [
        {"name": "record pass", "conclusion": "FAILURE", "completedAt": "2026-10-07T17:00:01Z"},
        {"name": GATE, "conclusion": gate},
    ]
    raw = {
        "state": "OPEN",
        "gated": True,
        "commits": [{"committedDate": "2026-10-07T17:00:00Z"}],
        "statusCheckRollup": rollup,
        "checkSuites": [FINISHED_RUN],
    }
    pull = ledger_events.pull_request(raw)
    run(store, recorded())
    run(store, in_pr(), now_ms=pull.red_at + 20 * MINUTE, github=lambda url: pull)
    assert len(texts(store, ENG_SEAT)) == notices


def test_a_red_notice_adds_to_the_notices_of_earlier_pull_requests(store):
    red, closed = answer("open_red"), answer("closed_unmerged")
    urls = {"https://github.com/o/r/pull/8": closed, "https://github.com/o/r/pull/9": red}
    tasks = [
        {"id": f"t{n}", "title": "Build", "state": "pr", "claimed_by": "sw-eng-1", "pr_url": url}
        for n, url in enumerate(urls)
    ]
    run(store, recorded())
    told = run(store, recorded(tasks=tasks), now_ms=red.red_at + 20 * MINUTE, github=urls.get)
    assert f"told {ENG_SEAT}: https://github.com/o/r/pull/8:closed" in told
    assert f"told {ENG_SEAT}: https://github.com/o/r/pull/9:red:" in told


URL = "https://github.com/o/r/pull/9"


def red_notice(store):
    red = answer("open_red")
    run(store, recorded())
    run(store, in_pr(), now_ms=red.red_at + 20 * MINUTE, github=lambda url: red)
    [item] = InboxStore(store.redis).inbox(ENG_SEAT)
    return red, item


def red_index(store):
    return store.redis.hgetall(store.key("sw", "red-notices"))


def done_task():
    return recorded(tasks=[{"id": "t1", "title": "Build the thing", "state": "done", "pr_url": URL}])


@pytest.mark.parametrize(
    ("settled", "reason"),
    [
        (answer("merged"), "merged"),
        (answer("closed_unmerged"), "closed"),
        (ledger_events.PullRequest("OPEN", None, 1, False, resolved=True, head="green"), "turned green"),
    ],
)
def test_a_red_notice_closes_done_when_its_pull_request_settles(store, settled, reason):
    red, item = red_notice(store)
    assert red_index(store) == {item.id: URL}
    asked = []
    told = run(store, done_task(), now_ms=red.red_at + 21 * MINUTE, github=lambda u: asked.append(u) or settled)
    inbox = InboxStore(store.redis)
    closed = inbox.get(item.id)
    assert (closed.state, closed.reason) == ("done", f"done: pull request {URL} {reason}")
    assert f"closed the red notice {item.id}: {URL} {reason}" in told
    assert asked == [URL]
    last = inbox.history(item.id)[-1]
    assert (last["state"], last["by"], last["reason"]) == ("done", "swarm", f"done: pull request {URL} {reason}")
    assert red_index(store) == {}


@pytest.mark.parametrize(
    "still",
    [
        answer("open_red"),
        ledger_events.PullRequest("OPEN", None, 1, False, resolved=False, head="running"),
        ledger_events.PullRequest("OPEN", None, 1, True, resolved=True, head="red"),
        None,
    ],
)
def test_a_red_notice_stays_open_while_its_pull_request_is_red_or_unknown(store, still):
    red, item = red_notice(store)
    told = run(store, done_task(), now_ms=red.red_at + 21 * MINUTE, github=lambda url: still)
    assert InboxStore(store.redis).get(item.id).state == "pending"
    assert not [line for line in told if line.startswith("closed the red notice")]
    assert red_index(store) == {item.id: URL}


def test_a_red_notice_closes_after_its_engineer_record_is_dropped_and_other_notices_stay(store):
    red, item = red_notice(store)
    store.drop_agent("sw", "sw-eng-1")
    inbox = InboxStore(store.redis)
    other = inbox.send("swarm", ENG_SEAT, "Your wait on checks has ended.")
    stranger = inbox.send("sw-eng-2", ENG_SEAT, item.text)
    run(store, done_task(), now_ms=red.red_at + 21 * MINUTE, github=lambda url: answer("merged"))
    assert inbox.get(item.id).state == "done"
    assert inbox.get(other.id).state == "pending"
    assert inbox.get(stranger.id).state == "pending"


def test_a_red_notice_closed_by_its_reader_leaves_the_index_unasked(store):
    red, item = red_notice(store)
    inbox = InboxStore(store.redis)
    inbox.close(item.id, "swarm", "done", "fixed")
    asked = []
    run(store, done_task(), now_ms=red.red_at + 21 * MINUTE, github=lambda url: asked.append(url) or answer("merged"))
    assert inbox.get(item.id).reason == "done: fixed"
    assert asked == []
    assert red_index(store) == {}


def test_a_red_notice_whose_close_races_stays_indexed_and_closes_next_pass(store, monkeypatch):
    red, item = red_notice(store)
    store.redis.hset(store.key("sw", "red-notices"), "gone", URL)

    def changed_meanwhile(self, item_id, closer, kind, detail=""):
        raise ledger_events.InboxError(f"message {item_id} changed meanwhile")

    with monkeypatch.context() as patched:
        patched.setattr(InboxStore, "close", changed_meanwhile)
        told = run(store, done_task(), now_ms=red.red_at + 21 * MINUTE, github=lambda url: answer("merged"))
    assert not [line for line in told if line.startswith("closed the red notice")]
    assert red_index(store) == {item.id: URL}
    told = run(store, done_task(), now_ms=red.red_at + 22 * MINUTE, github=lambda url: answer("merged"))
    assert told == [f"closed the red notice {item.id}: {URL} merged"]
    assert red_index(store) == {}


def test_open_red_notices_sent_before_the_index_are_found_once_at_their_seats(store):
    inbox = InboxStore(store.redis)
    text = f"Your pull request {URL} for task t1 (Build the thing) has red checks and no push for twenty minutes."
    legacy = inbox.send("swarm", "eng-7@sw", text)
    foreign = inbox.send("swarm", "eng-1@swx", text)
    stranger = inbox.send("sw-eng-2", ENG_SEAT, text)
    done = inbox.send("swarm", ENG_SEAT, text.replace("pull/9", "pull/8"))
    inbox.close(done.id, "swarm", "done", "fixed")
    asked = []
    told = run(store, done_task(), github=lambda url: asked.append(url) or answer("merged"))
    assert told == [f"closed the red notice {legacy.id}: {URL} merged"]
    assert asked == [URL]
    assert [inbox.get(i.id).state for i in (legacy, foreign, stranger)] == ["done", "pending", "pending"]
    assert store.redis.exists(store.key("sw", "events-sent", "red-notices:backfill"))
    late = inbox.send("swarm", "eng-7@sw", text)
    run(store, done_task(), github=lambda url: answer("merged"))
    assert inbox.get(late.id).state == "pending"


def test_the_backfill_indexes_only_open_red_notices_from_the_swarm(store):
    inbox = InboxStore(store.redis)
    text = f"Your pull request {URL} for task t1 (Build the thing) has red checks and no push for twenty minutes."
    legacy = inbox.send("swarm", ENG_SEAT, text)
    inbox.send("sw-eng-2", ENG_SEAT, text)
    inbox.send("swarm", ENG_SEAT, "Your wait on checks has ended.")
    closed = inbox.send("swarm", ENG_SEAT, text)
    inbox.close(closed.id, "swarm", "done", "fixed")
    ledger_events._backfill_red_notices(ledger_events.Mail(inbox, store, "sw"))
    assert red_index(store) == {legacy.id: URL}


def test_a_red_pull_request_does_not_hold_back_the_notices_after_it(store):
    red, item = red_notice(store)
    inbox = InboxStore(store.redis)
    later = "https://github.com/o/r/pull/8"
    second = inbox.send("swarm", ENG_SEAT, item.text.replace(URL, later))
    store.redis.hset(store.key("sw", "red-notices"), second.id, later)
    views = {URL: red, later: answer("merged")}
    run(store, done_task(), now_ms=red.red_at + 21 * MINUTE, github=views.get)
    assert (inbox.get(item.id).state, inbox.get(second.id).state) == ("pending", "done")


def test_a_gone_notice_does_not_hold_back_the_notices_after_it(store):
    inbox = InboxStore(store.redis)
    run(store, recorded())
    store.redis.hset(store.key("sw", "red-notices"), "gone", URL)
    after = inbox.send("swarm", ENG_SEAT, "Your pull request was red.")
    store.redis.hset(store.key("sw", "red-notices"), after.id, URL)
    run(store, done_task(), github=lambda url: answer("merged"))
    assert inbox.get(after.id).state == "done"
    assert red_index(store) == {}


def test_one_pass_asks_github_once_per_pull_request(store):
    red, item = red_notice(store)
    asked = []
    run(store, in_pr(), now_ms=red.red_at + 21 * MINUTE, github=lambda url: asked.append(url) or red)
    assert asked == [URL]


def test_an_old_commit_pushed_now_is_not_red_before_twenty_minutes_from_its_push_and_red(store):
    raw = json.loads((ANSWERS / "open_red.json").read_text())
    raw["commits"][0]["committedDate"] = "2026-10-04T23:40:12Z"
    red = ledger_events.pull_request(raw)
    run(store, recorded())
    run(store, in_pr(), now_ms=red.red_at + 20 * MINUTE - 1, github=lambda url: red)
    assert texts(store, ENG_SEAT) == []
    run(store, in_pr(), now_ms=red.red_at + 20 * MINUTE, github=lambda url: red)
    assert len(texts(store, ENG_SEAT)) == 1


def test_the_red_window_starts_at_the_later_of_the_push_and_the_red_result():
    window = ledger_events.RED_QUIET_MS
    assert ledger_events.red_window(100, 500, 500 + window - 1) is None
    assert ledger_events.red_window(100, 500, 500 + window) == 500
    assert ledger_events.red_window(900, 500, 900 + window - 1) is None
    assert ledger_events.red_window(900, 500, 900 + window) == 900
    assert ledger_events.red_window(900, None, 900 + window) == 900
    assert ledger_events.red_window(None, 500, 500 + window) == 500
    assert ledger_events.red_window(None, None, 10**15) is None


def test_the_push_is_the_earliest_workflow_run_and_the_red_result_the_earliest_red_check():
    raw = json.loads((ANSWERS / "open_red.json").read_text())
    raw["checkSuites"].append(
        {"status": "COMPLETED", "workflowRun": {"databaseId": 1, "createdAt": "2026-10-05T01:50:00Z"}}
    )
    raw["statusCheckRollup"].append({"context": "ci/external", "state": "ERROR", "createdAt": "2026-10-05T01:40:21Z"})
    found = ledger_events.pull_request(raw)
    assert found.pushed_at == ledger_events.iso_ms("2026-10-05T01:40:19Z")
    assert found.red_at == ledger_events.iso_ms("2026-10-05T01:40:21Z")
    raw["checkSuites"] = [{"status": "QUEUED", "workflowRun": None}]
    raw["commits"][0]["committedDate"] = "2026-10-04T23:40:12Z"
    unrun = ledger_events.pull_request(raw)
    assert unrun.pushed_at == ledger_events.iso_ms("2026-10-04T23:40:12Z")
    assert ledger_events.red_window(unrun.pushed_at, unrun.red_at, unrun.red_at + 20 * MINUTE - 1) is None


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


def test_a_pull_request_names_its_failed_checks():
    assert answer("open_red").failed == ("unit (3.11, 1, early)",)
    assert answer("merged").failed == ()
    raw = {
        "state": "OPEN",
        "statusCheckRollup": [
            {"context": "ci/status", "state": "ERROR"},
            {"conclusion": "TIMED_OUT"},
            {"name": "lint", "conclusion": "SUCCESS"},
        ],
    }
    assert ledger_events.pull_request(raw).failed == ("ci/status", "a check")


@pytest.mark.parametrize("entry, queued", [(None, False), ({"id": "queue-entry"}, True)])
def test_a_pull_request_reports_merge_queue_membership(entry, queued):
    raw = {"state": "OPEN", "mergeQueueEntry": entry}
    assert ledger_events.pull_request(raw).queued is queued


SKIPPED_ONLY = [{"name": "ledger-equivalence", "conclusion": "SKIPPED"}]
QUEUED_TESTS = {"status": "QUEUED", "workflowRun": {"databaseId": 1}}
FINISHED_RUN = {"status": "COMPLETED", "workflowRun": {"databaseId": 2}}
APP_SUITE = {"status": "QUEUED", "workflowRun": None}


@pytest.mark.parametrize("status", ["QUEUED", "IN_PROGRESS", "WAITING", "PENDING", "REQUESTED"])
def test_a_head_with_only_skipped_checks_and_an_unfinished_run_stays_unresolved(status):
    raw = {
        "state": "OPEN",
        "statusCheckRollup": SKIPPED_ONLY,
        "checkSuites": [FINISHED_RUN, {"status": status, "workflowRun": {"databaseId": 1}}],
    }
    pull = ledger_events.pull_request(raw)
    assert pull.resolved is False
    assert pull.red is False


@pytest.mark.parametrize(
    "rollup",
    [
        SKIPPED_ONLY,
        [{"name": "lint", "conclusion": "SUCCESS"}],
        [{"name": "unit", "conclusion": "SUCCESS"}, {"name": "lint", "conclusion": None}],
    ],
)
def test_passing_checks_do_not_resolve_while_a_run_on_the_head_is_queued(rollup):
    raw = {"state": "OPEN", "statusCheckRollup": rollup, "checkSuites": [QUEUED_TESTS]}
    assert ledger_events.pull_request(raw).resolved is False


@pytest.mark.parametrize(
    ("rollup", "red"),
    [
        (SKIPPED_ONLY + [{"name": "unit", "conclusion": "SUCCESS"}], False),
        (SKIPPED_ONLY + [{"name": "unit", "conclusion": "FAILURE"}], True),
    ],
)
def test_a_head_whose_runs_all_finished_resolves_green_or_red(rollup, red):
    raw = {"state": "OPEN", "statusCheckRollup": rollup, "checkSuites": [FINISHED_RUN, APP_SUITE]}
    pull = ledger_events.pull_request(raw)
    assert pull.resolved is True
    assert pull.red is red


def test_a_pending_check_keeps_a_finished_head_unresolved():
    raw = {
        "state": "OPEN",
        "statusCheckRollup": [{"name": "unit", "conclusion": "SUCCESS"}, {"name": "lint", "conclusion": None}],
    }
    assert ledger_events.pull_request(raw).resolved is False


@pytest.mark.parametrize("conclusion", ["FAILURE", "TIMED_OUT"])
def test_a_failed_check_waits_for_the_queued_run_before_resolving_red(conclusion):
    raw = {
        "state": "OPEN",
        "statusCheckRollup": [{"name": "unit", "conclusion": conclusion}],
        "checkSuites": [QUEUED_TESTS],
    }
    pull = ledger_events.pull_request(raw)
    assert pull.resolved is False
    assert pull.red is True
    raw["checkSuites"] = [FINISHED_RUN]
    assert ledger_events.pull_request(raw).resolved is (conclusion == "FAILURE")


@pytest.mark.parametrize("suites", [[], [QUEUED_TESTS], [FINISHED_RUN, APP_SUITE]])
@pytest.mark.parametrize("gated", [True, False])
def test_a_head_with_only_skipped_checks_keeps_waiting(suites, gated):
    raw = {"state": "OPEN", "gated": gated, "statusCheckRollup": SKIPPED_ONLY, "checkSuites": suites}
    pull = ledger_events.pull_request(raw)
    assert pull.resolved is (gated and suites != [QUEUED_TESTS])
    assert pull.red is (gated and suites != [QUEUED_TESTS])
    raw["statusCheckRollup"] = SKIPPED_ONLY + [{"name": GATE, "conclusion": "SUCCESS"}]
    assert ledger_events.pull_request(raw).resolved is (suites != [QUEUED_TESTS])


@pytest.mark.parametrize("gate", [None, "PENDING", "CANCELLED", "TIMED_OUT"])
def test_a_gate_that_has_not_passed_or_failed_keeps_the_head_unresolved(gate):
    rollup = SKIPPED_ONLY + [{"name": "unit", "conclusion": "SUCCESS"}, {"name": GATE, "conclusion": gate}]
    raw = {"state": "OPEN", "gated": True, "statusCheckRollup": rollup, "checkSuites": [FINISHED_RUN]}
    assert ledger_events.pull_request(raw).resolved is False


def test_a_passed_gate_resolves_green_once_nothing_is_pending():
    rollup = SKIPPED_ONLY + [{"name": GATE, "conclusion": "SUCCESS"}]
    raw = {"state": "OPEN", "gated": True, "statusCheckRollup": rollup, "checkSuites": [FINISHED_RUN, APP_SUITE]}
    pull = ledger_events.pull_request(raw)
    assert pull.resolved is True
    assert pull.red is False
    raw["checkSuites"] = [QUEUED_TESTS]
    assert ledger_events.pull_request(raw).resolved is False
    raw["checkSuites"] = [FINISHED_RUN]
    raw["statusCheckRollup"] = rollup + [{"name": "sonar", "conclusion": None}]
    assert ledger_events.pull_request(raw).resolved is False
    for state in ("PENDING", "EXPECTED", ""):
        raw["statusCheckRollup"] = rollup + [{"context": "sonar", "state": state}]
        assert ledger_events.pull_request(raw).resolved is False
    raw["statusCheckRollup"] = rollup + [{"name": "sonar", "conclusion": "CANCELLED"}]
    assert ledger_events.pull_request(raw).resolved is True


@pytest.mark.parametrize("suites", [[FINISHED_RUN], [QUEUED_TESTS]])
@pytest.mark.parametrize("conclusion", ["FAILURE", "ERROR", "STARTUP_FAILURE", "ACTION_REQUIRED"])
def test_a_failed_gate_resolves_red(suites, conclusion):
    rollup = SKIPPED_ONLY + [{"name": GATE, "conclusion": conclusion}]
    raw = {"state": "OPEN", "gated": True, "statusCheckRollup": rollup, "checkSuites": suites}
    pull = ledger_events.pull_request(raw)
    assert pull.resolved is True
    assert pull.red is True
    assert pull.failed == (GATE,)


@pytest.mark.parametrize(
    ("gated", "gate", "passed"), [(True, "SUCCESS", True), (True, "FAILURE", False), (False, "SUCCESS", False)]
)
def test_the_view_records_whether_the_required_gate_passed_beside_a_red_job(gated, gate, passed):
    rollup = [{"name": "record pass", "conclusion": "FAILURE"}, {"name": GATE, "conclusion": gate}]
    raw = {"state": "OPEN", "gated": gated, "statusCheckRollup": rollup, "checkSuites": [FINISHED_RUN]}
    pull = ledger_events.pull_request(raw)
    assert (pull.red, pull.resolved, pull.gate_passed) == (True, True, passed)
    raw["statusCheckRollup"] = [rollup[0], {"context": GATE, "state": gate}]
    assert ledger_events.pull_request(raw).gate_passed is passed
    raw["statusCheckRollup"] = rollup + [{"name": GATE, "conclusion": "SUCCESS"}]
    assert ledger_events.pull_request(raw).gate_passed is passed


def test_a_failed_check_beside_a_pending_gate_resolves_red_once_runs_finish():
    rollup = [{"name": "unit", "conclusion": "FAILURE"}, {"name": GATE, "conclusion": None}]
    raw = {"state": "OPEN", "gated": True, "statusCheckRollup": rollup, "checkSuites": [FINISHED_RUN]}
    assert ledger_events.pull_request(raw).resolved is True
    raw["checkSuites"] = [QUEUED_TESTS]
    assert ledger_events.pull_request(raw).resolved is False


def test_a_gate_check_counts_only_by_its_exact_name():
    rollup = [{"name": "Gate — Required later", "conclusion": "SUCCESS"}]
    raw = {"state": "OPEN", "gated": True, "statusCheckRollup": rollup, "checkSuites": [FINISHED_RUN]}
    assert ledger_events.pull_request(raw).unpassed_gate == GATE
    raw["statusCheckRollup"] = rollup + [{"context": GATE, "state": "SUCCESS"}]
    assert ledger_events.pull_request(raw).resolved is True


@pytest.mark.parametrize("rollup", [[], SKIPPED_ONLY, [{"name": GATE, "conclusion": "SKIPPED"}]])
def test_a_dead_required_gate_resolves_without_changing_red_reminder_fields(rollup):
    raw = {"state": "OPEN", "gated": True, "statusCheckRollup": rollup, "checkSuites": [FINISHED_RUN]}
    pull = ledger_events.pull_request(raw)
    assert pull.resolved is True
    assert pull.unpassed_gate == GATE
    assert pull.red is True
    assert pull.failed == ()
    assert pull.red_at is None


@pytest.mark.parametrize("gated", [False, True])
@pytest.mark.parametrize("gate", ["SUCCESS", None, "PENDING", "CANCELLED", "TIMED_OUT"])
def test_an_unpassed_gate_is_only_a_missing_or_skipped_terminal_gate(gated, gate):
    raw = {"state": "OPEN", "gated": gated, "statusCheckRollup": [{"context": GATE, "state": gate}]}
    assert ledger_events.pull_request(raw).unpassed_gate == ""


@pytest.mark.parametrize("pending", [None, "", "PENDING", "EXPECTED"])
def test_a_skipped_gate_with_a_pending_check_does_not_end_the_wait(pending):
    raw = {
        "state": "OPEN",
        "gated": True,
        "statusCheckRollup": [{"name": GATE, "conclusion": "SKIPPED"}, {"context": "lint", "state": pending}],
    }
    pull = ledger_events.pull_request(raw)
    assert pull.resolved is False
    assert pull.unpassed_gate == ""


@pytest.mark.parametrize("gate", [[], [{"name": GATE, "conclusion": "SKIPPED"}]])
@pytest.mark.parametrize(
    "failure", [[], [{"name": "unit", "conclusion": "FAILURE", "completedAt": "2026-10-07T17:00:01Z"}]]
)
def test_dead_gate_resolution_is_red_but_reminders_still_require_actual_failure(store, gate, failure):
    raw = {
        "state": "OPEN",
        "gated": True,
        "commits": [{"committedDate": "2026-10-07T17:00:00Z"}],
        "statusCheckRollup": gate + failure,
    }
    pull = ledger_events.pull_request(raw)
    assert pull.resolved is True
    assert pull.red is True
    assert pull.unpassed_gate == GATE
    run(store, recorded())
    now = (pull.red_at or pull.pushed_at) + 20 * MINUTE
    run(store, in_pr(), now_ms=now, github=lambda url: pull)
    expected = (
        [
            "Your pull request https://github.com/o/r/pull/9 for task t1 (Build the thing) "
            "has red checks and no push for twenty minutes. Fix them and push."
        ]
        if failure
        else []
    )
    assert texts(store, ENG_SEAT) == expected
