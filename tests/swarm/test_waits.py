import json

import pytest

from scripts.inbox.store import InboxError, InboxStore
from scripts.swarm import cli, idle, waits
from scripts.swarm.ledger_events import PullRequest
from scripts.swarm.store import AgentRecord, RedisStore, SwarmError
from tests.swarm.test_cli import env, run  # noqa: F401

pytestmark = pytest.mark.xdist_group("fakeredis")

ME = "engineer@a1b2c3-0001"
URL = "https://github.com/o/r/pull/7"


@pytest.fixture
def started(env, monkeypatch):  # noqa: F811
    store, ledger, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    monkeypatch.setattr(cli, "now_ms", lambda: 1_000)
    ledger.tasks = lambda slug: list(ledger.rows.values()) if slug == "sw" else []
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
        "on": {"kind": "checks", "target": URL},
    }
    assert json.loads(capsys.readouterr().out) == {
        "agent": ME,
        "until": "1970-01-01T12:00:01+00:00",
        "on": {"kind": "checks", "target": URL},
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
    assert "wait on one of: checks, reply, task" in capsys.readouterr().err


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
        idle.declare_wait(store.redis, "sw", ME, 10_000_000, "", 1, on={"kind": kind, "target": target})

    def end(rows=None):
        return waits.end_pass(store, "sw", rows or {}, inbox, pulls.get)

    def told():
        return [item.text for item in inbox.inbox("eng-1@sw")]

    rig = type("Rig", (), {})()
    rig.store, rig.inbox, rig.pulls, rig.hold, rig.end, rig.told = store, inbox, pulls, hold, end, told
    return rig


@pytest.mark.parametrize(
    "pull, outcome",
    [
        (PullRequest("OPEN", None, 1, False, True), f"checks on {URL}, now green"),
        (PullRequest("OPEN", None, 1, True, True), f"checks on {URL}, now red"),
        (PullRequest("MERGED", 2, 1, False, True), f"pull request {URL}, now merged"),
        (PullRequest("CLOSED", None, 1, False, False), f"pull request {URL}, now closed"),
    ],
)
def test_the_tick_ends_a_checks_wait_once_and_tells_the_agent(tick, pull, outcome):
    tick.hold("checks", URL)
    tick.pulls[URL] = pull
    assert tick.end() == [f"ended the wait of {ME}: {outcome}"]
    assert idle.wait(tick.store.redis, "sw", ME) is None
    assert tick.told() == [
        f"Your wait on {outcome} has ended. Pick task t1 back up: agentihooks swarm sw done, block, "
        "or wait on the next thing."
    ]
    assert tick.end() == []
    assert len(tick.told()) == 1


@pytest.mark.parametrize("pull", [None, PullRequest("OPEN", None, 1, False, False)])
def test_a_checks_wait_stays_while_checks_run_or_github_cannot_answer(tick, pull):
    tick.hold("checks", URL)
    if pull:
        tick.pulls[URL] = pull
    assert tick.end() == []
    assert idle.wait(tick.store.redis, "sw", ME)["on"]["target"] == URL
    assert tick.told() == []


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
    with pytest.raises(SwarmError, match="wait on one of: checks, reply, task"):
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


def test_inbox_wait_cannot_also_wait_on_checks(started):
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

    def failed(*args):
        raise InboxError("store unavailable")

    monkeypatch.setattr(receive, "receive", failed)
    assert run("sw", "--as", ME, "wait", "--inbox") == 1
    assert held(store) is None
