from scripts.gates import intent
from scripts.swarm import cli
from tests.swarm.test_cli import env, run  # noqa: F401
from tests.swarm.test_delivery import FakeHerdr


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

    real = cli.tick

    def tick(*args):
        ticking.set()
        return real(*args)

    monkeypatch.setattr(intent, "Check", Waiting)
    monkeypatch.setattr(cli, "tick", tick)
    actions = cli.run_tick(store, "sw", ledger, rt, FakeHerdr({}))
    assert seen == [True]
    assert "judged" in actions


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
