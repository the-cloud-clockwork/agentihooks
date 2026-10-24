from scripts.swarm import cli
from tests.swarm.test_cli import env, run  # noqa: F401
from tests.swarm.test_delivery import FakeHerdr


def test_the_tick_runs_the_overrun_pass_on_its_live_agents(env, monkeypatch):  # noqa: F811
    store, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    monkeypatch.setattr(cli, "now_ms", lambda: 9_000_000)
    rt.live = {"engineer@a1b2c3-0001"}
    seen = []
    monkeypatch.setattr(cli.overrun, "overrun_pass", lambda watch, doc: seen.append((watch, doc)) or ["overrun seen"])
    actions = cli.run_tick(store, "sw", ledger, rt, FakeHerdr({}))
    [(watch, doc)] = seen
    assert "overrun seen" in actions
    assert (watch.slug, watch.store, watch.now_ms) == ("sw", store, 9_000_000)
    assert "engineer@a1b2c3-0001" in watch.live and doc["tasks"] == ledger.state("sw")["tasks"]
