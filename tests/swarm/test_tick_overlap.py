from scripts.gates import intent
from scripts.swarm import cli
from tests.swarm.test_cli import env, run  # noqa: F401
from tests.swarm.test_delivery import FakeHerdr
from tests.swarm.test_tick_groups import _Marker, _with


def test_the_intent_check_runs_while_the_tick_runs(env, monkeypatch):  # noqa: F811
    import threading

    store, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    ticking, seen = threading.Event(), []

    class Waiting:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, doc):
            seen.append(ticking.wait(10))
            return ["judged"]

    real = cli.TickSites._start

    def start(sites):
        ticking.set()
        return real(sites)

    monkeypatch.setattr(intent, "Check", Waiting)
    monkeypatch.setattr(cli.TickSites, "_start", start)
    actions = cli.run_tick(store, "sw", ledger, rt, FakeHerdr({}))
    assert seen == [True]
    assert "judged" in actions


def test_an_intent_failure_still_reaches_the_pass_when_the_tick_fails(env, monkeypatch):  # noqa: F811
    store, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    judged, ticked = RuntimeError("intent failed"), ValueError("tick failed")

    class Failing:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, doc):
            raise judged

    def tick(get):
        get("box")["raised"] = ticked
        return []

    monkeypatch.setattr(intent, "Check", Failing)
    _with(monkeypatch, _Marker("failing marker", "agents", tick))
    actions = cli.run_tick(store, "sw", ledger, rt, FakeHerdr({}))
    assert actions == [f"pass failed: a step raised RuntimeError: {judged}"]
    assert judged.__context__ is ticked


def test_the_tick_hands_the_intent_check_batched_pull_request_readers_and_a_limit(env, monkeypatch):  # noqa: F811
    from scripts.swarm import ledger_events

    store, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    built = []

    class Recorded:
        def __init__(self, *args, **kwargs):
            built.append((args, kwargs))

        def run(self, doc):
            return []

    monkeypatch.setattr(intent, "Check", Recorded)
    monkeypatch.setattr(ledger_events, "tick_view", lambda *args: lambda url: object())
    monkeypatch.setattr(ledger_events, "pull_texts", lambda urls, source=None: {})
    cli.run_tick(store, "sw", ledger, rt, FakeHerdr({}))
    [(args, kwargs)] = built
    assert isinstance(args[2], int)
    assert kwargs["limit"] == intent.JUDGED_PER_PASS
    assert kwargs["head"]("https://github.com/o/r/pull/1") is None


def test_the_later_pull_request_snapshot_reuses_the_earlier_one(env, monkeypatch):  # noqa: F811
    from scripts.swarm import ledger_events

    store, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    calls, built = [], []

    def tick_view(*args):
        calls.append(args)
        built.append(lambda url: None)
        return built[-1]

    monkeypatch.setattr(ledger_events, "tick_view", tick_view)
    cli.run_tick(store, "sw", ledger, rt, FakeHerdr({}))
    assert [len(args) for args in calls] == [4, 5]
    assert calls[1][4] is built[0]
