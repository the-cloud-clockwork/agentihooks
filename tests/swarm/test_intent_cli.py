import json

import pytest

from scripts.gates import intent
from scripts.gates.verdicts import Verdicts
from scripts.inbox.store import InboxStore
from scripts.swarm import cli
from tests.swarm.test_cli import env, run  # noqa: F401

pytestmark = pytest.mark.xdist_group("fakeredis")

ME = "engineer@a1b2c3-0001"
URL = "https://github.com/o/r/pull/9"


@pytest.fixture
def started(env, monkeypatch):  # noqa: F811
    store, ledger, _ = env
    ledger.phases = [{"id": "p1", "title": "Gates", "description": "Stop failures."}]
    ledger.rows["t1"].update(phase="p1", title="Intent", description="Refuse a failed merge.")
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    stamped = []
    monkeypatch.setattr(intent, "stamp_body", lambda url, doc, task, run=None: stamped.append((url, doc, task)) or True)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", ME)
    return store, ledger, stamped


@pytest.mark.parametrize("autonomy,awaiting", [("assist", "approval"), ("delegate", "")])
def test_swarm_pr_arms_the_intent_check_and_stamps_the_body(started, capsys, monkeypatch, autonomy, awaiting):
    store, ledger, stamped = started
    store.update("sw", autonomy=autonomy)
    calls, update, state = [], ledger.update_task, ledger.state
    ledger.update_task = lambda slug, task, fields, by="swarm": calls.append((slug, by)) or update(slug, task, fields)
    ledger.state = lambda slug: calls.append((slug, "state")) or state(slug)
    monkeypatch.setattr(cli, "now_ms", lambda: 4242)
    assert run("sw", "pr", URL) == 0
    out = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert out == {"task": "t1", "pr_url": URL, "intent": {"verdict": "pending", "body": True}}
    assert [ledger.rows["t1"][k] for k in ("state", "pr_url", "awaiting")] == ["pr", URL, awaiting]
    assert calls == [("sw", "state"), ("sw", ME)]
    [(url, doc, task)] = stamped
    assert (url, task["id"], task["title"], doc["phases"]) == (URL, "t1", "Intent", ledger.phases)
    assert Verdicts("sw", "intent").read("t1") == {"verdict": "pending", "reason": "intent check running", "at": 4242}


def test_swarm_pr_with_the_intent_gate_off_arms_nothing(started, capsys, monkeypatch):
    store, _, _ = started
    store.update("sw", gates={"intent": "off"})
    assert run("sw", "pr", URL) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["intent"] == {"verdict": "off", "body": True}
    assert Verdicts("sw", "intent").read("t1") is None


@pytest.mark.parametrize("mode", ["enforce", "observe", "off", "coach"])
def test_the_operator_sets_the_intent_gate_mode(env, monkeypatch, mode):  # noqa: F811
    store, _, _ = env
    monkeypatch.delenv("AGENTIHOOKS_AGENT_NAME", raising=False)
    run("sw", "create", "--repo", "/repo")
    assert run("sw", "set", f"intent-gate={mode}") == 0
    assert store.config("sw").gates == {"intent": mode}


def test_the_tick_returns_a_failed_task_to_its_agent_under_enforce(started, monkeypatch):
    store, ledger, _ = started
    store.update("sw", gates={"intent": "enforce"})
    ledger.rows["t1"].update(state="pr", pr_url=URL, claimed_by=ME)
    monkeypatch.setattr(intent, "pr_view", lambda url: {"title": "T", "body": "", "files": ["a.py"]})
    monkeypatch.setattr(intent, "judge", lambda state: ("fail", "the phase can use this change at probability 0.10"))
    actions = cli.run_tick(store, "sw")
    assert "task t1 intent check fail" in actions
    assert ledger.rows["t1"]["state"] == "claimed"
    task, text, by = [c for c in ledger.comments if c[0] == "t1"][-1]
    assert (text, by) == (intent.FAIL_COMMENT, "swarm")
    items = InboxStore(store.redis).inbox(next(a.seat for a in store.agents("sw") if a.name == ME))
    assert any(item.text.startswith("The intent check failed") and item.ref == "tasks/t1" for item in items)


def test_the_tick_reads_each_pull_request_once_for_all_its_passes(started, monkeypatch):
    from scripts.gates import progress
    from scripts.swarm import done_gate, ledger_events, priority_sweep, waits

    store, _, _ = started
    seen = {}

    def capture(name, index):
        def run(*args):
            seen[name] = args[index]
            args[index](URL)
            return []

        return run

    monkeypatch.setattr(ledger_events, "event_pass", capture("events", 6))
    monkeypatch.setattr(done_gate, "recheck_pass", capture("recheck", 5))
    monkeypatch.setattr(progress, "checks_pass", capture("checks", 3))
    monkeypatch.setattr(waits, "end_pass", capture("waits", 4))
    monkeypatch.setattr(priority_sweep, "priority_pass", capture("priority", 5))
    reads = []
    monkeypatch.setattr(ledger_events, "view", lambda url: reads.append(url))
    cli.run_tick(store, "sw")
    assert sorted(seen) == ["checks", "events", "priority", "recheck", "waits"]
    assert reads == [URL]
    store.redis.delete(store.key("sw", "tick-lock"))
    cli.run_tick(store, "sw")
    assert reads == [URL, URL]


def test_the_tick_batches_active_tasks_and_red_notices_then_refreshes_the_next_tick(started, monkeypatch):
    from scripts.gates.verdicts import Verdicts
    from scripts.inbox.store import InboxStore
    from scripts.swarm import ledger_events

    store, ledger, _ = started
    store.update("sw", gates={"intent": "coach"})
    ledger.rows["t1"].update(state="pr", pr_url=URL, claimed_by=ME)
    other = "https://github.com/another/repo/pull/2"
    notice = InboxStore(store.redis).send("swarm", "eng-1@sw", "red checks")
    store.redis.hset(store.key("sw", "red-notices"), notice.id, other)
    Verdicts("sw", "intent-coach").write("t1", "pass", "ok", 1, coach_rounds=0, head="h1", url=URL)
    batches, heads = [], []

    def batch(urls, cache=None):
        assert cache is store.redis
        batches.append(set(urls))
        head = "h1" if len(batches) == 1 else "h2"
        return {url: ledger_events.PullRequest("OPEN", None, None, False, head=head) for url in urls}

    monkeypatch.setattr(ledger_events, "views", batch, raising=False)
    monkeypatch.setattr(ledger_events, "view", lambda url: pytest.fail("individual remote read"))
    monkeypatch.setattr(intent, "pr_view", lambda url: heads.append(url))
    cli.run_tick(store, "sw")
    assert heads == []
    cli.run_tick(store, "sw")
    assert batches == [{URL, other}, {URL, other}]
    assert heads == [URL]


def test_the_coach_tick_keeps_an_unchanged_head_from_the_ticks_pull_request_read(started, monkeypatch):
    from scripts.gates.verdicts import Verdicts

    store, ledger, _ = started
    store.update("sw", gates={"intent": "coach"})
    ledger.rows["t1"].update(state="pr", pr_url=URL, claimed_by=ME)
    Verdicts("sw", "intent-coach").write("t1", "pass", "ok", 1, coach_rounds=0, head="h1", url=URL)
    from scripts.swarm import ledger_events

    reads, views = [], []
    pull = ledger_events.PullRequest("OPEN", None, None, False, head="h1")
    monkeypatch.setattr(ledger_events, "view", lambda url: reads.append(url) or pull)
    monkeypatch.setattr(intent, "pr_head", lambda url: pytest.fail("a second head read"))
    monkeypatch.setattr(intent, "pr_view", lambda url: views.append(url))
    actions = cli.run_tick(store, "sw")
    assert (reads, views) == ([URL], [])
    assert not any("intent check" in action for action in actions)
    assert Verdicts("sw", "intent").read("t1")["head"] == "h1"


@pytest.mark.parametrize("head", [None, ""])
def test_the_coach_tick_reads_the_whole_pull_request_when_the_ticks_read_has_no_head(started, monkeypatch, head):
    from scripts.gates.verdicts import Verdicts
    from scripts.swarm import ledger_events

    store, ledger, _ = started
    store.update("sw", gates={"intent": "coach"})
    ledger.rows["t1"].update(state="pr", pr_url=URL, claimed_by=ME)
    Verdicts("sw", "intent-coach").write("t1", "pass", "ok", 1, coach_rounds=0, head="h1", url=URL)
    pull = None if head is None else ledger_events.PullRequest("OPEN", None, None, False, head=head)
    monkeypatch.setattr(ledger_events, "view", lambda url: pull)
    views = []
    monkeypatch.setattr(intent, "pr_view", lambda url: views.append(url))
    cli.run_tick(store, "sw")
    assert views == [URL]
    assert Verdicts("sw", "intent").read("t1")["verdict"] != "pass"


def test_a_refused_ledger_write_after_the_tick_step_leaves_the_rest_running(started, monkeypatch, capsys):
    from scripts.swarm import priority_sweep
    from scripts.swarm.ledger_client import LedgerRefused

    store, ledger, _ = started
    store.update("sw", gates={"intent": "enforce"})
    ledger.rows["t1"].update(state="pr", pr_url=URL, claimed_by=ME)
    monkeypatch.setattr(intent, "pr_view", lambda url: {"title": "T", "body": "", "files": ["a.py"]})
    monkeypatch.setattr(intent, "judge", lambda state: ("fail", "the phase can use this change at probability 0.10"))

    def refuse(*args, **kwargs):
        raise LedgerRefused("ledger sw: server refused: 400 comment refused")

    swept = []
    monkeypatch.setattr(ledger, "comment", refuse)
    monkeypatch.setattr(priority_sweep, "priority_pass", lambda *args: swept.append(args) or [])
    store.redis.delete(store.key("sw", "last-tick"))
    actions = cli.run_tick(store, "sw")
    assert "skipped scripts.gates.intent.Check.run: the ledger refused its write" in actions
    assert swept
    assert store.redis.get(store.key("sw", "last-tick"))
    assert "the ledger refused its write" in capsys.readouterr().err
    items = InboxStore(store.redis).inbox(next(a.seat for a in store.agents("sw") if a.name == ME))
    assert any(item.text.startswith("The intent check failed") for item in items)


def test_the_tick_leaves_the_task_alone_under_the_default_observe(started, monkeypatch):
    store, ledger, _ = started
    ledger.rows["t1"].update(state="pr", pr_url=URL, claimed_by=ME)
    monkeypatch.setattr(intent, "pr_view", lambda url: {"title": "T", "body": "", "files": []})
    monkeypatch.setattr(intent, "judge", lambda state: ("fail", "no"))
    assert "task t1 intent check fail" in cli.run_tick(store, "sw")
    assert ledger.rows["t1"]["state"] == "pr"
    assert Verdicts("sw", "intent").read("t1")["verdict"] == "fail"


def test_the_tick_stamps_its_own_time_on_the_verdict(started, monkeypatch):
    store, ledger, _ = started
    ledger.rows["t1"].update(state="pr", pr_url=URL, claimed_by=ME)
    monkeypatch.setattr(intent, "pr_view", lambda url: {"title": "T", "body": "", "files": []})
    monkeypatch.setattr(intent, "judge", lambda state: ("pass", "ok"))
    monkeypatch.setattr(cli, "now_ms", lambda: 777)
    cli.run_tick(store, "sw")
    assert Verdicts("sw", "intent").read("t1") == {"verdict": "pass", "reason": "ok", "at": 777, "phase": "p1"}
