import threading
from typing import Any

import pytest

from scripts.gates import intent
from scripts.gates.verdicts import Verdicts
from scripts.swarm import cli
from scripts.swarm.ledger_events import PullRequest
from tests.swarm.test_cli import env, run  # noqa: F401
from tests.swarm.test_delivery import FakeHerdr


def test_ready_intent_is_delivered_before_a_slow_runtime_launch(env, monkeypatch):  # noqa: F811
    store, ledger, runtime = env
    run("sw", "create", "--repo", "/repo")
    store.update("sw", state="running", gates={"intent": "observe"})
    url = "https://github.com/o/r/pull/9"
    ledger.rows["t1"].update(
        state="pr", pr_url=url, title="Ready intent", description="Judge this pull request promptly."
    )
    ledger.pulls[url] = PullRequest("OPEN", None, None, False, head="h1")
    clock, delivered, at_launch = [1_000_000], [], []
    launching = threading.Event()
    monkeypatch.setattr(cli, "now_ms", lambda: clock[0])

    def view(url):
        launching.wait()
        return {"title": "Ready", "body": "", "files": [], "head": "h1"}

    monkeypatch.setattr(intent, "pr_view", view)

    def judge(state):
        delivered.append(clock[0])
        return {"verdict": "pass", "reason": "the phase can use it"}

    launch = runtime.spawn
    intent_pass = cli._intent_pass
    passed = threading.Event()

    def observed_pass(*args):
        try:
            return intent_pass(*args)
        finally:
            passed.set()

    def slow_launch(*args, **kwargs):
        launching.set()
        passed.wait()
        at_launch.append(Verdicts("sw", "intent").read("t1"))
        clock[0] += 780_000
        return launch(*args, **kwargs)

    monkeypatch.setattr(intent, "judged", judge)
    monkeypatch.setattr(runtime, "spawn", slow_launch)
    monkeypatch.setattr(cli, "_intent_pass", observed_pass)
    cli.run_tick(store, "sw", ledger, runtime, FakeHerdr({}))
    wait_ms = delivered[0] - 1_000_000
    print(f"ready_to_verdict_ms={wait_ms}; runtime_launch_delay_ms=780000")
    assert at_launch and all(record and record["verdict"] == "pass" for record in at_launch)
    assert wait_ms == 0


@pytest.mark.feature("swarm-intent-check-budget")
def test_the_intent_pass_returns_while_its_view_is_still_building(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from scripts.swarm import ledger_events

    release = threading.Event()

    def slow_view(*args: object) -> str:
        release.wait(10)
        return "view"

    monkeypatch.setattr(ledger_events, "tick_view", slow_view)
    monkeypatch.setattr(ledger_events, "Mail", lambda *args: None)
    config = SimpleNamespace(gates={"intent": "off"})
    returned: list[tuple[Any, list[str]]] = []
    caller = threading.Thread(
        target=lambda: returned.append(cli._intent_pass(None, None, "sw", {"tasks": []}, None, config))
    )
    caller.start()
    caller.join(5)
    finished = not caller.is_alive()
    release.set()
    caller.join(5)
    view, actions = returned[0]
    assert finished
    assert actions == []
    assert view.result(5) == "view"


def test_a_merge_wait_observes_a_merge_during_a_slow_runtime_launch(env, monkeypatch):  # noqa: F811
    from scripts.swarm import idle

    store, ledger, runtime = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    store.update("sw", max_eng=2, gates={"intent": "off"})
    agent = next(a for a in store.agents("sw") if a.lane == "eng")
    url = "https://github.com/o/r/pull/9"
    ledger.rows[agent.task].update(state="pr", pr_url=url)
    ledger.rows["t3"] = {"id": "t3", "state": "open", "lane": "eng", "claimed_by": "", "out_of_scope": False}
    ledger.pulls[url] = PullRequest("OPEN", None, None, False, queued=True)
    clock = [cli.now_ms()]
    monkeypatch.setattr(cli, "now_ms", lambda: clock[0])
    idle.declare_wait(
        store.redis, "sw", agent.name, clock[0] + 3_600_000, "", clock[0], {"kind": "merge", "target": url}
    )
    launch = runtime.spawn

    def slow_launch(*args, **kwargs):
        clock[0] += 780_000
        ledger.pulls[url] = PullRequest("MERGED", clock[0], None, False)
        return launch(*args, **kwargs)

    monkeypatch.setattr(runtime, "spawn", slow_launch)
    actions = cli.run_tick(store, "sw", ledger, runtime, FakeHerdr({}))
    assert any(f"ended the wait of {agent.name}: pull request {url}, now merged" == action for action in actions)
    assert idle.wait(store.redis, "sw", agent.name) is None


@pytest.mark.feature("swarm-intent-check-budget")
def test_the_intent_pass_returns_before_the_pass_view_is_built(env, monkeypatch):  # noqa: F811
    from scripts.inbox.store import InboxStore
    from scripts.swarm import ledger_events

    store, ledger, _ = env
    run("sw", "create", "--repo", "/repo")
    built, release = threading.Event(), threading.Event()
    lookup = object()

    def slow_view(*args):
        release.wait(5)
        built.set()
        return lookup

    monkeypatch.setattr(ledger_events, "tick_view", slow_view)
    monkeypatch.setattr(ledger_events, "pull_texts", lambda urls, source=None: {})
    doc = {"tasks": [], "phases": []}
    view, actions = cli._intent_pass(InboxStore(store.redis), store, "sw", doc, ledger, store.config("sw"))
    returned_before = not built.is_set()
    release.set()
    assert returned_before
    assert actions == []
    assert view.result(timeout=5) is lookup
