import json

import pytest

from scripts.inbox.store import InboxError, InboxStore
from scripts.swarm import cli, idle, waits
from scripts.swarm.ledger_events import PullRequest
from scripts.swarm.ledger_events import view as github_view
from scripts.swarm.store import AgentRecord, RedisStore, SwarmError
from tests.swarm.test_cli import env, run  # noqa: F401
from tests.swarm.test_ledger_events import APP_SUITE, QUEUED_TESTS, SKIPPED_ONLY

pytestmark = pytest.mark.xdist_group("fakeredis")

ME = "engineer@a1b2c3-0001"
URL = "https://github.com/o/r/pull/7"
NO_SUITES = {"nodes": [], "pageInfo": {"hasNextPage": False}}


@pytest.fixture
def started(env, monkeypatch):  # noqa: F811
    store, ledger, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    monkeypatch.setattr(cli, "now_ms", lambda: 1_000)
    ledger.tasks = lambda slug: list(ledger.rows.values()) if slug == "sw" else []
    monkeypatch.setattr(cli.ledger_events, "view", lambda url: PullRequest("MERGED", 2, 1, False, head="first"))
    return store, ledger


def held(store):
    return idle.wait(store.redis, "sw", ME)


def test_a_wait_on_checks_lasts_until_the_tick_ends_it(started, capsys):
    store, _ = started
    assert run("sw", "--as", ME, "wait", "--on", "checks", URL, "--reason", "tests") == 0
    assert held(store) == {
        "until": 1_000 + waits.CHECKED_MINUTES * 60_000,
        "reason": "tests",
        "at": 1_000,
        "on": {"kind": "checks", "target": URL, "head": "first"},
    }
    assert json.loads(capsys.readouterr().out) == {
        "agent": ME,
        "until": "1970-01-01T12:00:01+00:00",
        "on": {"kind": "checks", "target": URL, "head": "first"},
    }


def test_the_wait_usage_names_its_kind_and_target(capsys):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["sw", "wait", "--help"])
    assert "--on KIND TARGET" in capsys.readouterr().out


def test_minutes_bound_a_checked_wait(started):
    store, _ = started
    assert run("sw", "--as", ME, "wait", "90", "--on", "task", "t2") == 0
    assert held(store)["until"] == 1_000 + 90 * 60_000


def test_a_wait_on_a_reply_names_an_inbox_item_that_exists(started, capsys):
    store, _ = started
    item = InboxStore(store.redis).send(ME, "master@sw", "which way?")
    assert run("sw", "--as", ME, "wait", "--on", "reply", item.id) == 0
    assert held(store)["on"] == {"kind": "reply", "target": item.id}
    assert run("sw", "--as", ME, "wait", "--on", "reply", "nosuchitem") == 1
    assert "no inbox item nosuchitem" in capsys.readouterr().err


def test_checked_wait_targets_are_checked(started, capsys):
    assert run("sw", "--as", ME, "wait", "--on", "checks", "7") == 1
    assert "a pull request url" in capsys.readouterr().err
    assert run("sw", "--as", ME, "wait", "--on", "task", "t9") == 1
    assert "no task t9 on the ledger" in capsys.readouterr().err
    assert run("sw", "--as", ME, "wait", "--on", "task", "t1") == 1
    assert "task t1 is your own task" in capsys.readouterr().err
    assert run("sw", "--as", ME, "wait", "--on", "deploy", "x") == 1
    assert "wait on one of: checks, merge, reply, task" in capsys.readouterr().err


def test_a_bare_wait_is_capped_at_sixty_minutes(started, capsys):
    store, _ = started
    assert run("sw", "--as", ME, "wait", "1") == 0
    assert held(store)["until"] == 1_000 + 60_000
    assert run("sw", "--as", ME, "wait", "60") == 0
    assert held(store)["until"] == 1_000 + 60 * 60_000 and "on" not in held(store)
    assert run("sw", "--as", ME, "wait", "61") == 1
    assert "a bare wait lasts at most 60 minutes" in capsys.readouterr().err
    assert run("sw", "--as", ME, "wait") == 1
    assert "a wait lasts a whole number of minutes above zero" in capsys.readouterr().err


@pytest.mark.parametrize(
    "on, outcome", [(("checks", URL), f"pull request {URL}, now merged"), (("task", "t2"), "task t2, now done")]
)
def test_the_tick_ends_a_checked_wait_whose_thing_resolved(started, on, outcome):
    from tests.swarm.test_delivery import FakeHerdr
    from tests.swarm.test_tick import FakeRuntime

    store, ledger = started
    assert run("sw", "--as", ME, "wait", "--on", *on) == 0
    ledger.rows["t2"].update(state="done")
    actions = cli.run_tick(store, "sw", ledger, FakeRuntime(), FakeHerdr({}))
    assert f"ended the wait of {ME}: {outcome}" in actions
    assert held(store) is None


def test_target_problem_reads_each_kind():
    rows = {"t2": {"id": "t2", "state": "open"}, "t3": {"id": "t3", "state": "done"}}

    def get(item_id):
        if item_id != "abc":
            raise InboxError(f"no message {item_id}")

    assert waits.target_problem("checks", URL, "t1", rows, get) == ""
    assert waits.target_problem("checks", "http://github.com/o/r/pull/7", "t1", rows, get)
    assert waits.target_problem("checks", URL + "/files", "t1", rows, get)
    assert waits.target_problem("task", "t2", "t1", rows, get) == ""
    assert waits.target_problem("task", "t3", "t1", rows, get) == "task t3 is already done"
    assert waits.target_problem("reply", "abc", "t1", rows, get) == ""


@pytest.fixture
def tick():
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    inbox = InboxStore(store.redis)
    store.put_agent("sw", AgentRecord(name=ME, lane="eng", task="t1", seat="eng-1@sw"))
    pulls = {}

    def hold(kind, target):
        held = {"kind": kind, "target": target, **({"head": "first"} if kind == "checks" else {})}
        idle.declare_wait(store.redis, "sw", ME, 10_000_000, "", 1, on=held)

    fresh = {}

    def reread(url):
        return fresh[url] if url in fresh else pulls.get(url)

    def end(rows=None, now=5_000):
        return waits.end_pass(store, "sw", rows or {}, inbox, pulls.get, now, reread)

    def told():
        return [item.text for item in inbox.inbox("eng-1@sw")]

    rig = type("Rig", (), {})()
    rig.store, rig.inbox, rig.pulls, rig.hold, rig.end, rig.told = store, inbox, pulls, hold, end, told
    rig.fresh = fresh
    return rig


@pytest.mark.parametrize(
    "pull, outcome",
    [
        (PullRequest("OPEN", None, 1, False, True, head="first"), f"checks on {URL}, now green"),
        (PullRequest("OPEN", None, 1, True, True, head="first"), f"checks on {URL}, now red"),
        (PullRequest("MERGED", 2, 1, False, True), f"pull request {URL}, now merged"),
        (PullRequest("CLOSED", None, 1, False, False), f"pull request {URL}, now closed"),
    ],
)
def test_the_tick_ends_a_checks_wait_once_and_tells_the_agent(tick, pull, outcome):
    tick.hold("checks", URL)
    tick.pulls[URL] = pull
    assert tick.end() == [f"ended the wait of {ME}: {outcome}"]
    assert idle.wait(tick.store.redis, "sw", ME) is None
    assert idle.waited(tick.store.redis, "sw", ME) == 5_000
    assert idle.BEAT_TTL_S - 5 < tick.store.redis.ttl(idle.key("sw", "waited", ME)) <= idle.BEAT_TTL_S
    assert tick.told() == [
        f"Your wait on {outcome} has ended. Pick task t1 back up: agentihooks swarm sw done, block, "
        "or wait on the next thing."
    ]
    assert tick.end() == []
    assert len(tick.told()) == 1


@pytest.mark.parametrize("pull", [None, PullRequest("OPEN", None, 1, False, False, head="first")])
def test_a_checks_wait_stays_while_checks_run_or_github_cannot_answer(tick, pull):
    tick.hold("checks", URL)
    if pull:
        tick.pulls[URL] = pull
    assert tick.end() == []
    assert idle.wait(tick.store.redis, "sw", ME)["on"]["target"] == URL
    assert tick.told() == []


@pytest.mark.parametrize("state", ["OPEN", "CLOSED"])
def test_a_merge_wait_ends_red_when_the_queued_pull_request_drops_out(tick, state):
    tick.hold("merge", URL)
    tick.pulls[URL] = PullRequest("OPEN", None, 1, False, resolved=True, queued=True)
    assert tick.end() == []
    tick.pulls[URL] = None
    assert tick.end() == []
    tick.pulls[URL] = PullRequest(state, None, 1, False, resolved=True)
    assert tick.end() == [
        f"ended the wait of {ME}: pull request {URL}, now red; left the merge queue without merging; fix it and queue it again"
    ]
    assert held(tick.store) is None
    assert "now red" in tick.told()[0]
    assert "fix it and queue it again" in tick.told()[0]
    assert tick.end() == []


def test_the_tick_accepts_a_merge_wait_only_after_the_pull_request_lands(tick):
    tick.hold("merge", URL)
    tick.pulls[URL] = PullRequest("OPEN", None, 1, False, resolved=True, queued=True)
    assert tick.end() == []
    assert held(tick.store)["on"] == {"kind": "merge", "target": URL, "queued": True}
    tick.pulls[URL] = PullRequest("MERGED", 2, 1, False)
    assert tick.end() == [f"ended the wait of {ME}: pull request {URL}, now merged"]
    assert held(tick.store) is None
    assert f"Your wait on pull request {URL}, now merged has ended." in tick.told()[0]
    assert tick.end() == []


LEFT = f"pull request {URL}, now red; left the merge queue without merging; fix it and queue it again"
UNLISTED = PullRequest("OPEN", None, 1, False, resolved=True, head="first")
LISTED = PullRequest("OPEN", None, 1, False, resolved=True, head="first", queued=True)


def test_a_queue_read_that_lags_right_after_enqueue_keeps_the_merge_wait(tick):
    tick.hold("merge", URL)
    tick.pulls[URL] = UNLISTED
    tick.fresh[URL] = LISTED
    assert tick.end() == []
    assert held(tick.store)["on"] == {"kind": "merge", "target": URL, "queued": True}
    assert tick.told() == []


def test_a_fresh_entry_not_yet_visible_stays_queued_until_its_grace_runs_out(tick):
    tick.hold("merge", URL)
    tick.pulls[URL] = UNLISTED
    assert tick.end() == []
    assert tick.end(now=waits.FRESH_MS) == []
    assert held(tick.store)["on"] == {"kind": "merge", "target": URL}
    assert tick.end(now=1 + waits.FRESH_MS) == [f"ended the wait of {ME}: {LEFT}"]
    assert held(tick.store) is None


def test_a_fresh_entry_grace_lasts_five_minutes():
    assert waits.FRESH_MS == 300_000


@pytest.mark.parametrize(
    "pull, fresh, outcome",
    [
        (None, PullRequest("MERGED", 2, 1, False), ""),
        (PullRequest("MERGED", 2, 1, False), LISTED, f"pull request {URL}, now merged"),
        (LISTED, UNLISTED, ""),
    ],
)
def test_the_merge_wait_rereads_only_an_open_pull_request_the_queue_does_not_list(pull, fresh, outcome):
    on = {"kind": "merge", "target": URL}
    assert waits.resolution(on, {}, None, {URL: pull}.get, {URL: fresh}.get, False) == outcome
    assert on == {"kind": "merge", "target": URL, **({"queued": True} if pull is LISTED else {})}


def test_a_seen_entry_gets_no_fresh_grace_once_the_queue_drops_it():
    on = {"kind": "merge", "target": URL}
    assert waits.resolution(on, {}, None, {URL: UNLISTED}.get, {URL: UNLISTED}.get, True) == ""
    assert waits.resolution(on, {}, None, {URL: UNLISTED}.get, {URL: UNLISTED}.get, False) == LEFT
    on["queued"] = True
    assert waits.resolution(on, {}, None, {URL: UNLISTED}.get, {URL: UNLISTED}.get, True) == LEFT


def test_a_merge_wait_holds_while_the_unlisted_pull_request_still_awaits_checks(tick):
    tick.hold("merge", URL)
    tick.pulls[URL] = LISTED
    assert tick.end() == []
    tick.pulls[URL] = PullRequest("OPEN", None, 1, False, resolved=False, head="first")
    assert tick.end() == []
    assert held(tick.store)["on"] == {"kind": "merge", "target": URL, "queued": True}
    assert tick.told() == []


def test_a_pull_request_removed_after_a_failed_group_reports_left_once_the_reread_agrees(tick):
    tick.hold("merge", URL)
    tick.pulls[URL] = LISTED
    assert tick.end() == []
    tick.pulls[URL] = UNLISTED
    tick.fresh[URL] = None
    assert tick.end() == []
    tick.fresh[URL] = UNLISTED
    assert tick.end() == [f"ended the wait of {ME}: {LEFT}"]
    assert held(tick.store) is None


def test_a_reread_that_finds_the_pull_request_merged_reports_merged(tick):
    tick.hold("merge", URL)
    tick.pulls[URL] = UNLISTED
    tick.fresh[URL] = PullRequest("MERGED", 2, 1, False)
    assert tick.end() == [f"ended the wait of {ME}: pull request {URL}, now merged"]


def test_the_tick_rereads_the_queue_with_an_uncached_view(started, monkeypatch):
    from tests.swarm.test_delivery import FakeHerdr
    from tests.swarm.test_tick import FakeRuntime

    store, ledger = started
    assert run("sw", "--as", ME, "wait", "--on", "merge", URL) == 0
    monkeypatch.setattr(cli.ledger_events, "tick_view", lambda *args: {URL: UNLISTED}.get)
    monkeypatch.setattr(cli.ledger_events, "view", {URL: LISTED}.get)
    cli.run_tick(store, "sw", ledger, FakeRuntime(), FakeHerdr({}))
    assert held(store)["on"] == {"kind": "merge", "target": URL, "queued": True}


def test_cli_records_a_checked_merge_wait_with_a_pull_request_url(started, capsys):
    store, _ = started
    assert run("sw", "--as", ME, "wait", "--on", "merge", URL) == 0
    assert held(store)["on"] == {"kind": "merge", "target": URL}
    assert run("sw", "--as", ME, "wait", "--on", "merge", "bad-url") == 1
    assert "wait on merge needs a pull request url" in capsys.readouterr().err


@pytest.mark.parametrize(
    "check, outcome",
    [
        ({"conclusion": "CANCELLED"}, ""),
        ({"conclusion": "SKIPPED"}, "green"),
        ({"conclusion": "NEUTRAL"}, ""),
        ({"conclusion": "STALE"}, ""),
        ({"conclusion": ""}, ""),
        ({"status": "IN_PROGRESS"}, ""),
        ({"state": "PENDING"}, ""),
        ({"state": "EXPECTED"}, ""),
        ({"conclusion": "FAILURE"}, "red"),
        ({"conclusion": "ERROR"}, "red"),
        ({"conclusion": "TIMED_OUT"}, ""),
        ({"conclusion": "STARTUP_FAILURE"}, "red"),
        ({"conclusion": "ACTION_REQUIRED"}, "red"),
        ({"conclusion": "SUCCESS"}, "green"),
        ({"state": "SUCCESS"}, "green"),
    ],
)
@pytest.mark.parametrize("position", [0, 1])
def test_checks_wait_requires_success_or_failure_from_the_status_rollup(tick, check, outcome, position):
    from scripts.swarm.ledger_events import pull_request

    tick.hold("checks", URL)
    checks = [{"name": "lint", "conclusion": "SUCCESS"}]
    checks.insert(position, {"name": "mutation", **check})
    tick.pulls[URL] = pull_request({"state": "OPEN", "headRefOid": "first", "statusCheckRollup": checks})
    if not outcome:
        assert tick.end() == []
        assert idle.wait(tick.store.redis, "sw", ME)["on"] == {"kind": "checks", "target": URL, "head": "first"}
        assert tick.told() == []
        return
    assert tick.end() == [f"ended the wait of {ME}: checks on {URL}, now {outcome}"]
    assert idle.wait(tick.store.redis, "sw", ME) is None
    assert tick.told() == [
        f"Your wait on checks on {URL}, now {outcome} has ended. Pick task t1 back up: "
        "agentihooks swarm sw done, block, or wait on the next thing."
    ]


def test_checks_wait_with_no_checks_stays_unresolved(tick):
    from scripts.swarm.ledger_events import pull_request

    tick.hold("checks", URL)
    tick.pulls[URL] = pull_request({"state": "OPEN", "headRefOid": "first", "statusCheckRollup": []})
    assert tick.end() == []
    assert idle.wait(tick.store.redis, "sw", ME)["on"] == {"kind": "checks", "target": URL, "head": "first"}
    assert tick.told() == []


@pytest.mark.parametrize("unresolved", ["CANCELLED", "TIMED_OUT", "PENDING"])
@pytest.mark.parametrize("failure", ["FAILURE", "ERROR", "STARTUP_FAILURE", "ACTION_REQUIRED"])
def test_a_failure_ends_the_checks_wait_even_with_an_unresolved_check(tick, unresolved, failure):
    from scripts.swarm.ledger_events import pull_request

    tick.hold("checks", URL)
    tick.pulls[URL] = pull_request(
        {
            "state": "OPEN",
            "headRefOid": "first",
            "statusCheckRollup": [
                {"name": "mutation", "conclusion": unresolved},
                {"name": "lint", "conclusion": failure},
            ],
        }
    )
    assert tick.end() == [f"ended the wait of {ME}: checks on {URL}, now red"]
    assert idle.wait(tick.store.redis, "sw", ME) is None
    assert tick.told() == [
        f"Your wait on checks on {URL}, now red has ended. Pick task t1 back up: "
        "agentihooks swarm sw done, block, or wait on the next thing."
    ]


@pytest.mark.parametrize("state, ends", [("done", True), ("blocked", True), ("claimed", False), ("pr", False)])
def test_a_task_wait_ends_when_the_task_is_done_or_blocked(tick, state, ends):
    tick.hold("task", "t2")
    ended = tick.end({"t2": {"id": "t2", "state": state}})
    assert ended == ([f"ended the wait of {ME}: task t2, now {state}"] if ends else [])


def test_a_task_wait_ends_when_the_task_leaves_the_ledger(tick):
    tick.hold("task", "t2")
    assert tick.end({}) == [f"ended the wait of {ME}: task t2, gone from the ledger"]


def test_a_reply_wait_ends_when_its_item_closes(tick):
    item = tick.inbox.send(ME, "master@sw", "which way?")
    tick.hold("reply", item.id)
    assert tick.end() == []
    tick.inbox.reply(item.id, "master@sw", "left")
    assert tick.end() == [f"ended the wait of {ME}: message {item.id}, now done"]


def test_a_reply_wait_on_a_vanished_item_ends(tick):
    tick.hold("reply", "gone")
    assert tick.end() == [f"ended the wait of {ME}: message gone, gone from the inbox"]


def test_bare_waits_and_finished_agents_are_left_alone(tick):
    idle.declare_wait(tick.store.redis, "sw", ME, 10_000_000, "deploy", 1)
    assert tick.end() == []
    tick.hold("task", "t2")
    tick.store.put_agent("sw", AgentRecord(name=ME, lane="eng", task="t1", seat="eng-1@sw", state="finished"))
    assert tick.end({}) == []


def test_a_wait_end_is_kept_a_day_past_the_wait(tick):
    redis, day = tick.store.redis, idle.BEAT_TTL_S * 1000
    idle.declare_wait(redis, "sw", ME, 61_000, "deploy", 1_000)
    assert idle.waited(redis, "sw", ME) == 61_000
    assert day + 55_000 < redis.pttl(idle.key("sw", "waited", ME)) <= day + 60_000
    idle.end_wait(redis, "sw", ME, 2_000)
    assert (idle.wait(redis, "sw", ME), idle.waited(redis, "sw", ME)) == (None, 2_000)
    assert day - 5_000 < redis.pttl(idle.key("sw", "waited", ME)) <= day


@pytest.mark.parametrize(
    "entry, named",
    [
        ({"reason": "deploy"}, True),
        ({"reason": "", "on": {"kind": "task", "target": "t2"}}, True),
        ({"on": {"kind": "reply", "target": "abc"}}, True),
        ({"reason": "  "}, False),
        ({"reason": ""}, False),
        ({}, False),
    ],
)
def test_a_wait_is_named_by_its_target_or_its_reason(entry, named):
    assert idle.named(entry) is named


def test_a_skipped_agent_never_stops_the_pass_for_the_next(tick):
    bare, running, done = (f"engineer@a1b2c3-000{n}" for n in (0, 2, 3))
    for name in (bare, running, done):
        tick.store.put_agent("sw", AgentRecord(name=name, lane="eng", task="t1"))
    tick.store.drop_agent("sw", ME)
    idle.declare_wait(tick.store.redis, "sw", bare, 10_000_000, "deploy", 1)
    idle.declare_wait(tick.store.redis, "sw", running, 10_000_000, "", 1, on={"kind": "task", "target": "t2"})
    idle.declare_wait(tick.store.redis, "sw", done, 10_000_000, "", 1, on={"kind": "task", "target": "t3"})
    rows = {"t2": {"id": "t2", "state": "claimed"}, "t3": {"id": "t3", "state": "done"}}
    assert tick.end(rows) == [f"ended the wait of {done}: task t3, now done"]


def test_an_agent_without_a_seat_is_told_by_name(tick):
    tick.store.put_agent("sw", AgentRecord(name=ME, lane="eng", task="t1"))
    tick.hold("task", "t2")
    tick.end({})
    assert [item.text for item in tick.inbox.inbox(ME)][0].startswith("Your wait on task t2")


def test_cli_wait_refuses_a_checked_wait_for_an_unknown_kind():
    with pytest.raises(SwarmError, match="wait on one of: checks, merge, reply, task"):
        waits.on("deploy", "x")


def test_inbox_wait_returns_pending_seat_work_without_a_pane_prompt(started, capsys):
    store, _ = started
    capsys.readouterr()
    store.seats.occupy("master@sw", ME, at=1)
    inbox = InboxStore(store.redis)
    item = inbox.send("operator", "master@sw", "inspect the result")
    assert run("sw", "--as", ME, "wait", "--inbox") == 0
    result = json.loads(capsys.readouterr().out)
    returned = {entry["id"]: entry for entry in result["items"]}
    assert returned[item.id]["text"] == item.text
    assert result["timed_out"] is False
    assert held(store) is None


@pytest.mark.parametrize("minutes", ["0", "-1", "61"])
def test_inbox_wait_refuses_unbounded_duration(started, minutes, capsys, monkeypatch):
    from scripts.inbox import receive

    monkeypatch.setattr(receive, "receive", lambda *args: [])
    capsys.readouterr()
    assert run("sw", "--as", ME, "wait", minutes, "--inbox") == 1
    assert (
        capsys.readouterr().err
        == "swarm: an inbox wait takes one to sixty minutes and cannot also wait on a dependency\n"
    )


def test_inbox_wait_cannot_also_wait_on_checks(started, monkeypatch):
    from scripts.inbox import receive

    def unexpected(*args):
        pytest.fail("a conflicting dependency wait must be refused before receiving")

    monkeypatch.setattr(receive, "receive", unexpected)
    assert run("sw", "--as", ME, "wait", "--inbox", "--on", "checks", URL) == 1


def test_inbox_wait_timeout_and_failure_clear_the_declared_wait(started, monkeypatch, capsys):
    from scripts.inbox import receive

    store, _ = started
    capsys.readouterr()
    calls = []

    def empty(inbox, me, timeout):
        calls.append((me, timeout, held(store)))
        return []

    monkeypatch.setattr(receive, "receive", empty)
    assert run("sw", "--as", ME, "wait", "2", "--inbox") == 0
    assert json.loads(capsys.readouterr().out) == {"items": [], "timed_out": True}
    assert calls[0][:2] == (ME, 120)
    assert calls == [(ME, 120, {"until": 121_000, "reason": "inbox work", "at": 1_000})]
    calls.clear()
    assert run("sw", "--as", ME, "wait", "--inbox") == 0
    capsys.readouterr()
    assert calls == [(ME, 60, {"until": 61_000, "reason": "inbox work", "at": 1_000})]
    calls.clear()
    assert run("sw", "--as", ME, "wait", "60", "--inbox") == 0
    capsys.readouterr()
    assert calls == [(ME, 3600, {"until": 3_601_000, "reason": "inbox work", "at": 1_000})]
    assert held(store) is None
    assert idle.waited(store.redis, "sw", ME) == 1_000

    def failed(*args):
        raise InboxError("store unavailable")

    monkeypatch.setattr(receive, "receive", failed)
    assert run("sw", "--as", ME, "wait", "--inbox") == 1
    assert held(store) is None


@pytest.mark.parametrize(
    "command, action",
    [
        (("done", "--pr", URL), "done"),
        (("block", "needs a token"), "a block"),
        (("wait", "--on", "checks", URL), "a new wait"),
        (("progress", "--doing", "fixing", "--ends-when", "green"), "progress"),
    ],
)
def test_acting_on_the_task_closes_its_wait_ended_notice_with_the_action(started, command, action):
    from tests.swarm.test_delivery import FakeHerdr
    from tests.swarm.test_tick import FakeRuntime

    store, ledger = started
    inbox = InboxStore(store.redis)
    [agent] = [a for a in store.agents("sw") if a.name == ME]
    address = agent.seat or agent.name
    earlier = inbox.send("master@sw", address, "an unrelated question")
    assert run("sw", "--as", ME, "wait", "--on", "task", "t2") == 0
    ledger.rows["t2"].update(state="done")
    cli.run_tick(store, "sw", ledger, FakeRuntime(), FakeHerdr({}))
    [notice] = [item for item in inbox.inbox(address) if item.text.startswith("Your wait on")]
    inbox.deliver(notice.id, ME)
    quoted = inbox.send("master@sw", address, "You were told: Pick task t1 back up: then?")
    assert run("sw", "--as", ME, *command) == 0
    closed = inbox.get(notice.id)
    assert (closed.state, closed.reason) == ("done", f"done: {ME} recorded {action} on task t1")
    assert [inbox.get(item.id).state for item in (earlier, quoted)] == ["pending", "pending"]


def test_a_wait_ended_notice_for_another_task_stays_open(tick):
    tick.hold("task", "t2")
    tick.end({})
    [notice] = tick.inbox.inbox("eng-1@sw")
    tick.store.put_agent("sw", AgentRecord(name=ME, lane="eng", task="t11", seat="eng-1@sw"))
    [agent] = tick.store.agents("sw")
    waits.settle_notices(tick.inbox, agent, "progress")
    assert tick.inbox.get(notice.id).state == "pending"


def test_a_handoff_closes_its_wait_ended_notice_before_the_seat_passes_on(started, tmp_path):
    from tests.swarm.test_cli import _handoff_doc
    from tests.swarm.test_delivery import FakeHerdr
    from tests.swarm.test_tick import FakeRuntime

    store, ledger = started
    inbox = InboxStore(store.redis)
    [agent] = [a for a in store.agents("sw") if a.name == ME]
    assert run("sw", "--as", ME, "wait", "--on", "task", "t2") == 0
    ledger.rows["t2"].update(state="done")
    cli.run_tick(store, "sw", ledger, FakeRuntime(), FakeHerdr({}))
    [notice] = [item for item in inbox.inbox(agent.seat) if item.text.startswith("Your wait on")]
    assert waits.notice_task(notice) == "t1"
    inbox.deliver(notice.id, ME)
    assert run("sw", "--as", ME, "handoff", str(_handoff_doc(tmp_path))) == 0
    closed = inbox.get(notice.id)
    assert (closed.state, closed.reason) == ("done", f"done: {ME} recorded a handoff on task t1")


def test_notice_task_names_the_task_only_a_swarm_wait_ended_notice_asks_back():
    from scripts.inbox.store import Item

    text = f"Your wait on checks on {URL}, now red has ended. Pick task t13 back up: agentihooks swarm sw done"
    notice = Item(id="n", sender="swarm", address="eng-1@sw", text=text, state="delivered", created_at=1, updated_at=1)
    assert waits.notice_task(notice) == "t13"
    assert waits.notice_task(Item(**{**notice.__dict__, "sender": "master@sw"})) == ""
    assert waits.notice_task(Item(**{**notice.__dict__, "text": "Pick task t13 back up: then"})) == ""


def test_checks_declaration_records_the_remote_head(started, monkeypatch, capsys):
    from types import SimpleNamespace

    store, _ = started
    calls = []

    def view(url):
        calls.append(url)
        return SimpleNamespace(head="first")

    monkeypatch.setattr(cli.ledger_events, "view", view)
    assert run("sw", "--as", ME, "wait", "--on", "checks", URL) == 0
    assert held(store)["on"] == {"kind": "checks", "target": URL, "head": "first"}
    assert json.loads(capsys.readouterr().out)["on"] == held(store)["on"]
    assert calls == [URL]


@pytest.mark.parametrize("pull", [None, {"head": ""}])
def test_checks_declaration_refuses_an_unknown_head(started, monkeypatch, capsys, pull):
    from types import SimpleNamespace

    store, _ = started
    monkeypatch.setattr(cli.ledger_events, "view", lambda url: SimpleNamespace(**pull) if pull else None)
    assert run("sw", "--as", ME, "wait", "--on", "checks", URL) == 1
    assert held(store) is None
    assert capsys.readouterr().err == "swarm: cannot read the pull request head; retry the checks wait\n"


def test_pull_request_reads_the_head_commit():
    from scripts.swarm.ledger_events import pull_request

    assert pull_request({"state": "OPEN", "headRefOid": "first"}).head == "first"
    assert pull_request({"state": "OPEN"}).head == ""


@pytest.mark.parametrize("red", [False, True])
def test_a_new_head_resets_checks_before_current_head_resolution(tick, red):
    from types import SimpleNamespace

    tick.hold("checks", URL)
    tick.pulls[URL] = SimpleNamespace(state="OPEN", head="first", resolved=False, red=False, unpassed_gate="")
    assert tick.end() == []
    before = idle.wait(tick.store.redis, "sw", ME)
    assert before["on"]["head"] == "first"
    tick.pulls[URL] = SimpleNamespace(state="OPEN", head="second", resolved=True, red=red, unpassed_gate="")
    assert tick.end() == []
    after = idle.wait(tick.store.redis, "sw", ME)
    assert after == {**before, "on": {"kind": "checks", "target": URL, "head": "second"}}
    assert tick.told() == []
    outcome = "red" if red else "green"
    assert tick.end() == [f"ended the wait of {ME}: checks on {URL}, now {outcome}"]
    assert idle.wait(tick.store.redis, "sw", ME) is None


@pytest.mark.parametrize("confirmation", [None, ("second", True), ("first", False), ("", True)])
def test_a_push_or_missing_checks_during_resolution_keeps_the_wait(tick, confirmation):
    from types import SimpleNamespace

    tick.hold("checks", URL)
    current = SimpleNamespace(state="OPEN", head="first", resolved=False, red=False, unpassed_gate="")
    tick.pulls[URL] = current
    assert tick.end() == []
    current.resolved = True
    latest = (
        SimpleNamespace(state="OPEN", head=confirmation[0], resolved=confirmation[1], red=False, unpassed_gate="")
        if confirmation
        else None
    )
    replies = iter([current, latest])
    assert waits.end_pass(tick.store, "sw", {}, tick.inbox, lambda url: next(replies), 5_000, None) == []
    assert tick.told() == []
    assert idle.wait(tick.store.redis, "sw", ME)["on"]["head"] == (
        "second" if confirmation and confirmation[0] == "second" else "first"
    )


def test_the_probe_requests_the_head_with_its_check_rollup():
    from types import SimpleNamespace

    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "data": {
                        "resource": {
                            "state": "OPEN",
                            "headRefOid": "first",
                            "mergeQueueEntry": {"id": "entry"},
                            "commits": {
                                "nodes": [
                                    {
                                        "commit": {
                                            "committedDate": "2026-10-07T17:00:00Z",
                                            "statusCheckRollup": {
                                                "contexts": {
                                                    "nodes": [{"name": "lint", "conclusion": "SUCCESS"}],
                                                    "pageInfo": {"hasNextPage": False},
                                                }
                                            },
                                            "checkSuites": {"nodes": [], "pageInfo": {"hasNextPage": False}},
                                        }
                                    }
                                ]
                            },
                        }
                    }
                }
            ),
        )

    pull = github_view(URL, run)
    assert pull.head == "first"
    assert pull.resolved is True
    assert pull.red is False
    assert pull.pushed_at == 1791392400000
    assert pull.queued is True
    assert calls == [
        (
            [
                "gh",
                "api",
                "graphql",
                "-f",
                "query=query($url:URI!){resource(url:$url){...on PullRequest{state mergedAt headRefOid mergeQueueEntry{id} "
                "commits(last:1){nodes{commit{committedDate "
                'file(path:".github/workflows"){object{...on Tree{entries{object{...on Blob{text}}}}}} '
                "statusCheckRollup{contexts(first:100){"
                "nodes{...on CheckRun{name conclusion completedAt} ...on StatusContext{context state createdAt}} "
                "pageInfo{hasNextPage}}} checkSuites(first:100){nodes{status workflowRun{databaseId createdAt}} "
                "pageInfo{hasNextPage}}}}}}}}",
                "-f",
                f"url={URL}",
            ],
            {"capture_output": True, "text": True, "timeout": 20},
        )
    ]


@pytest.mark.parametrize(
    "commits",
    [[], [{"commit": {"committedDate": "2026-10-07T17:00:00Z", "statusCheckRollup": None, "checkSuites": NO_SUITES}}]],
)
def test_the_probe_without_checks_is_unresolved(commits):
    from types import SimpleNamespace

    raw = {"data": {"resource": {"state": "OPEN", "headRefOid": "first", "commits": {"nodes": commits}}}}
    pull = github_view(URL, lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(raw)))
    assert pull.head == "first"
    assert pull.resolved is False


def probe(rollup, suites, suites_more=False):
    from types import SimpleNamespace

    commit = {
        "committedDate": "2026-10-07T17:00:00Z",
        "statusCheckRollup": {"contexts": {"nodes": rollup, "pageInfo": {"hasNextPage": False}}},
        "checkSuites": {"nodes": suites, "pageInfo": {"hasNextPage": suites_more}},
    }
    raw = {"data": {"resource": {"state": "OPEN", "headRefOid": "second", "commits": {"nodes": [{"commit": commit}]}}}}
    return github_view(URL, lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(raw)))


def test_the_probe_keeps_a_head_with_only_skipped_checks_and_a_queued_run_unresolved():
    pull = probe(SKIPPED_ONLY, [QUEUED_TESTS, APP_SUITE])
    assert pull.resolved is False


@pytest.mark.parametrize("suites", [None, {"nodes": None, "pageInfo": {"hasNextPage": False}}, {"nodes": []}])
def test_the_probe_refuses_unreadable_check_suites(suites):
    from types import SimpleNamespace

    commit = {
        "committedDate": "2026-10-07T17:00:00Z",
        "statusCheckRollup": {"contexts": {"nodes": SKIPPED_ONLY, "pageInfo": {"hasNextPage": False}}},
        "checkSuites": suites,
    }
    raw = {"data": {"resource": {"state": "OPEN", "headRefOid": "second", "commits": {"nodes": [{"commit": commit}]}}}}
    assert github_view(URL, lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(raw))) is None


def test_the_probe_ignores_a_queued_suite_without_a_workflow_run():
    pull = probe(SKIPPED_ONLY + [{"name": "unit", "conclusion": "SUCCESS"}], [APP_SUITE])
    assert pull.resolved is True
    assert pull.red is False


def test_the_probe_refuses_a_partial_list_of_check_suites():
    assert probe(SKIPPED_ONLY, [APP_SUITE], suites_more=True) is None


def test_a_checks_wait_stays_held_while_the_new_heads_run_is_queued(tick):
    tick.hold("checks", URL)
    tick.pulls[URL] = probe(SKIPPED_ONLY, [QUEUED_TESTS])
    assert tick.end() == []
    assert tick.end() == []
    assert tick.told() == []
    assert idle.wait(tick.store.redis, "sw", ME)["on"]["head"] == "second"


GATE_WORKFLOW = "jobs:\n  gate-required:\n    name: Gate — Required\n    needs: [unit]\n"
UNIT_PASSED = SKIPPED_ONLY + [{"name": "unit", "conclusion": "SUCCESS"}]


def workflows(*texts):
    return {"object": {"entries": [{"name": f"w{n}.yml", "object": {"text": text}} for n, text in enumerate(texts)]}}


def gated_probe(rollup, suites, tree):
    from types import SimpleNamespace

    commit = {
        "committedDate": "2026-10-07T17:00:00Z",
        "statusCheckRollup": {"contexts": {"nodes": rollup, "pageInfo": {"hasNextPage": False}}},
        "checkSuites": {"nodes": suites, "pageInfo": {"hasNextPage": False}},
        "file": tree,
    }
    raw = {"data": {"resource": {"state": "OPEN", "headRefOid": "second", "commits": {"nodes": [{"commit": commit}]}}}}
    return github_view(URL, lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(raw)))


@pytest.mark.parametrize(
    "text",
    [
        GATE_WORKFLOW,
        "jobs:\n  gate:\n    name: 'Gate — Required'\n",
        'jobs:\n  gate:\n    name: "Gate — Required"  \n',
        "jobs:\n  gate:\n\tname:\tGate — Required\t\n\n",
    ],
)
def test_the_probe_reads_a_declared_gate_from_the_head_workflows(text):
    assert gated_probe(UNIT_PASSED, [], workflows("name: Docs\n", text)).unpassed_gate == "Gate — Required"


@pytest.mark.parametrize(
    "tree",
    [
        None,
        {"object": None},
        {"object": {"entries": None}},
        workflows("name: Docs\n", None),
        workflows("jobs:\n  gate:\n    name: Gate — Required later\n"),
        workflows("jobs:\n  gate:\n    # name: Gate — Required\n"),
        workflows("jobs:\n  gate:\n    name: 'Gate — Required\"\n"),
        {"object": {"entries": [{"name": "x.yml", "object": None}]}},
        workflows("name: Gate — Required\njobs:\n  unit:\n    runs-on: ubuntu-latest\n"),
        workflows("on: push\n\nname: Gate — Required\n"),
        workflows("jobs:\n  gate:\n    name:\n      Gate — Required\n"),
    ],
)
def test_the_probe_without_a_declared_gate_resolves_on_every_check(tree):
    pull = gated_probe(UNIT_PASSED, [], tree)
    assert pull.resolved is True
    assert pull.red is False


def test_the_probe_reads_the_gate_from_the_last_commit():
    from types import SimpleNamespace

    def commit(tree):
        return {
            "commit": {
                "committedDate": "2026-10-07T17:00:00Z",
                "statusCheckRollup": {"contexts": {"nodes": UNIT_PASSED, "pageInfo": {"hasNextPage": False}}},
                "checkSuites": NO_SUITES,
                "file": tree,
            }
        }

    nodes = [commit(None), commit(workflows(GATE_WORKFLOW))]
    raw = {"data": {"resource": {"state": "OPEN", "headRefOid": "second", "commits": {"nodes": nodes}}}}
    pull = github_view(URL, lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(raw)))
    assert pull.unpassed_gate == "Gate — Required"


@pytest.mark.parametrize(
    ("rollup", "outcome"),
    [
        (UNIT_PASSED, [f"checks on {URL}, now red; Gate — Required never passed"]),
        (UNIT_PASSED + [{"name": "Gate — Required", "conclusion": "SUCCESS"}], [f"checks on {URL}, now green"]),
        (UNIT_PASSED + [{"name": "Gate — Required", "conclusion": "FAILURE"}], [f"checks on {URL}, now red"]),
    ],
)
def test_a_gated_checks_wait_ends_only_on_its_gate(tick, rollup, outcome):
    tick.hold("checks", URL)
    tick.pulls[URL] = gated_probe(rollup, [], workflows(GATE_WORKFLOW))
    assert tick.end() == []
    assert tick.end() == [f"ended the wait of {ME}: {line}" for line in outcome]
    assert (idle.wait(tick.store.redis, "sw", ME) is None) is bool(outcome)


@pytest.mark.parametrize(
    "done",
    [
        (1, ""),
        (0, "{}"),
        (0, '{"data":{"resource":null}}'),
        (0, '{"data":{"resource":{"commits":null}}}'),
        (0, '{"data":{"resource":{"commits":{"nodes":null}}}}'),
        (0, "invalid json"),
        (
            0,
            '{"data":{"resource":{"state":"OPEN","headRefOid":"first","commits":{"nodes":[{"commit":{"committedDate":"2026-10-07T17:00:00Z","statusCheckRollup":{"contexts":{"nodes":[{"conclusion":"SUCCESS"}],"pageInfo":{"hasNextPage":true}}},"checkSuites":{"nodes":[],"pageInfo":{"hasNextPage":false}}}}]}}}}',
        ),
        (
            0,
            '{"data":{"resource":{"state":"OPEN","headRefOid":"first","commits":{"nodes":[{"commit":{"committedDate":"2026-10-07T17:00:00Z","statusCheckRollup":{"contexts":{"nodes":[{"conclusion":"SKIPPED"}],"pageInfo":{"hasNextPage":false}}}}}]}}}}',
        ),
    ],
)
def test_the_probe_refuses_failed_or_incomplete_reads(done):
    from types import SimpleNamespace

    assert github_view(URL, lambda *args, **kwargs: SimpleNamespace(returncode=done[0], stdout=done[1])) is None


@pytest.mark.parametrize("head", ["", None])
def test_a_tick_without_a_head_does_not_resolve_or_reset_the_wait(tick, head):
    from types import SimpleNamespace

    tick.hold("checks", URL)
    before = idle.wait(tick.store.redis, "sw", ME)
    tick.pulls[URL] = SimpleNamespace(state="OPEN", head=head, resolved=True, red=False, unpassed_gate="")
    assert tick.end() == []
    assert idle.wait(tick.store.redis, "sw", ME) == before
    assert tick.told() == []


def test_a_legacy_checks_wait_binds_before_resolving(tick, monkeypatch):
    from types import SimpleNamespace

    idle.declare_wait(tick.store.redis, "sw", ME, 10_000_000, "tests", 1, on={"kind": "checks", "target": URL})
    tick.pulls[URL] = SimpleNamespace(state="OPEN", head="first", resolved=True, red=False, unpassed_gate="")
    key = idle.key("sw", "wait", ME)
    ttl = tick.store.redis.pttl(key)
    assert tick.end() == []
    assert idle.wait(tick.store.redis, "sw", ME) == {
        "until": 10_000_000,
        "reason": "tests",
        "at": 1,
        "on": {"kind": "checks", "target": URL, "head": "first"},
    }
    assert 0 < tick.store.redis.pttl(key) <= ttl
    assert tick.end() == [f"ended the wait of {ME}: checks on {URL}, now green"]


def test_checks_resolution_uses_the_confirmed_current_head_result(tick):
    from types import SimpleNamespace

    tick.hold("checks", URL)
    replies = iter(
        [
            SimpleNamespace(state="OPEN", head="first", resolved=True, red=False, unpassed_gate=""),
            SimpleNamespace(state="OPEN", head="first", resolved=True, red=True, unpassed_gate=""),
        ]
    )
    assert waits.end_pass(tick.store, "sw", {}, tick.inbox, lambda url: next(replies), 5_000, None) == [
        f"ended the wait of {ME}: checks on {URL}, now red"
    ]


@pytest.mark.parametrize("head, resolved", [("second", True), ("first", True)])
def test_the_tick_preserves_a_wait_redeclared_during_its_probe(tick, head, resolved):
    from types import SimpleNamespace

    tick.hold("checks", URL)
    calls = []

    def github(url):
        calls.append(url)
        idle.declare_wait(
            tick.store.redis, "sw", ME, 20_000_000, "new wait", 2, on={"kind": "checks", "target": URL, "head": "third"}
        )
        return SimpleNamespace(state="OPEN", head=head, resolved=resolved, red=False, unpassed_gate="")

    assert waits.end_pass(tick.store, "sw", {}, tick.inbox, github, 5_000, None) == []
    assert idle.wait(tick.store.redis, "sw", ME) == {
        "until": 20_000_000,
        "reason": "new wait",
        "at": 2,
        "on": {"kind": "checks", "target": URL, "head": "third"},
    }
    assert tick.told() == []


def test_a_pending_current_head_does_not_rewrite_the_wait(tick, monkeypatch):
    from types import SimpleNamespace

    tick.hold("checks", URL)
    tick.pulls[URL] = SimpleNamespace(state="OPEN", head="first", resolved=False, red=False, unpassed_gate="")

    def unexpected_write(*args, **kwargs):
        pytest.fail("an unresolved wait with the same head needs no Redis write")

    monkeypatch.setattr(tick.store.redis, "transaction", unexpected_write)
    assert tick.end() == []


def test_a_replaced_wait_does_not_stop_resolution_for_the_next_agent(tick):
    from types import SimpleNamespace

    following = "engineer@a1b2c3-0002"
    tick.store.put_agent("sw", AgentRecord(name=following, lane="eng", task="t2", seat="eng-2@sw"))
    tick.hold("checks", URL)
    idle.declare_wait(tick.store.redis, "sw", following, 10_000_000, "", 1, on={"kind": "task", "target": "t3"})

    def github(url):
        idle.declare_wait(
            tick.store.redis, "sw", ME, 20_000_000, "", 2, on={"kind": "checks", "target": URL, "head": "third"}
        )
        return SimpleNamespace(state="OPEN", head="second", resolved=True, red=False, unpassed_gate="")

    assert waits.end_pass(tick.store, "sw", {"t3": {"state": "done"}}, tick.inbox, github, 5_000, None) == [
        f"ended the wait of {following}: task t3, now done"
    ]
    assert idle.wait(tick.store.redis, "sw", ME)["on"]["head"] == "third"
    assert idle.wait(tick.store.redis, "sw", following) is None


@pytest.mark.parametrize("rollup", [[], UNIT_PASSED, [{"name": "Gate — Required", "conclusion": "SKIPPED"}]])
def test_a_dead_required_gate_ends_the_wait_red_and_names_the_gate(tick, rollup):
    tick.hold("checks", URL)
    tick.pulls[URL] = gated_probe(rollup, [], workflows(GATE_WORKFLOW))
    assert tick.end() == []
    outcome = f"checks on {URL}, now red; Gate — Required never passed"
    assert tick.end() == [f"ended the wait of {ME}: {outcome}"]
    assert idle.wait(tick.store.redis, "sw", ME) is None
    assert tick.told() == [
        f"Your wait on {outcome} has ended. Pick task t1 back up: "
        "agentihooks swarm sw done, block, or wait on the next thing."
    ]
    assert tick.end() == []


@pytest.mark.parametrize("rollup", [[], UNIT_PASSED, [{"name": "Gate — Required", "conclusion": "SKIPPED"}]])
@pytest.mark.parametrize("status", ["QUEUED", "IN_PROGRESS"])
def test_a_dead_gate_keeps_waiting_while_its_workflow_run_is_active(tick, rollup, status):
    tick.hold("checks", URL)
    tick.pulls[URL] = gated_probe(
        rollup, [{"status": status, "workflowRun": {"databaseId": 1}}], workflows(GATE_WORKFLOW)
    )
    assert tick.end() == []
    assert tick.end() == []
    assert idle.wait(tick.store.redis, "sw", ME)["on"] == {"kind": "checks", "target": URL, "head": "second"}
    assert tick.told() == []
