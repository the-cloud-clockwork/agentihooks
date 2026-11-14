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


class _RecordedCheck:
    """An intent check that records how the pass built it and judges nothing."""

    built: list[dict[str, Any]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Record the keyword arguments the pass gave."""
        _RecordedCheck.built.append(kwargs)

    def run(self, doc: dict[str, Any]) -> list[str]:
        """Judge nothing."""
        return []


def _quiet_pass(monkeypatch: pytest.MonkeyPatch, view: Any) -> None:
    """Replace the pass view, the mail and the batched pull read with fakes."""
    from scripts.swarm import ledger_events

    monkeypatch.setattr(ledger_events, "tick_view", view)
    monkeypatch.setattr(ledger_events, "Mail", lambda *args: None)
    monkeypatch.setattr(ledger_events, "pull_texts", lambda batch: {})


@pytest.mark.feature("swarm-intent-check-budget")
def test_the_intent_pass_wires_one_pull_reader_and_the_tick_judging_into_its_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    _quiet_pass(monkeypatch, lambda *args: "view")
    modes: list[str] = []

    def watched(slug: str, doc: dict[str, Any], mode: str) -> list[str]:
        modes.append(mode)
        return []

    judging = object()
    _RecordedCheck.built = []
    monkeypatch.setattr(intent, "watched_urls", watched)
    monkeypatch.setattr(intent, "Check", _RecordedCheck)
    monkeypatch.setattr(intent, "TICK_JUDGING", judging)
    view, actions = cli._intent_pass(None, None, "sw", {"tasks": []}, None, SimpleNamespace(gates={"intent": "coach"}))
    [built] = _RecordedCheck.built
    reader = built["head"].__self__
    assert view.result(5) == "view" and actions == []
    assert modes == ["coach"]
    assert built["judging"] is judging
    assert isinstance(reader, intent.Pulls)
    for name in ("text", "merged", "draft"):
        assert built[name].__self__ is reader
        assert built[name].__func__ is getattr(intent.Pulls, name)


@pytest.mark.feature("swarm-intent-check-budget")
def test_the_intent_pass_builds_its_view_timed_inside_the_callers_context(monkeypatch: pytest.MonkeyPatch) -> None:
    from contextvars import ContextVar
    from types import SimpleNamespace

    from scripts.swarm import timing

    probe: ContextVar[str] = ContextVar("probe", default="outside")
    timed: list[Any] = []
    call = timing.call

    def recorded(function: Any, *args: Any, **kwargs: Any) -> Any:
        timed.append(function)
        return call(function, *args, **kwargs)

    def view(*args: Any) -> str:
        return probe.get()

    _quiet_pass(monkeypatch, view)
    monkeypatch.setattr(timing, "call", recorded)
    probe.set("inside")
    built, _ = cli._intent_pass(None, None, "sw", {"tasks": []}, None, SimpleNamespace(gates={"intent": "off"}))
    assert built.result(5) == "inside"
    assert view in timed


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
