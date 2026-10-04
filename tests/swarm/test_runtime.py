from types import SimpleNamespace

from scripts.swarm.runtime import HerdrRuntime


def test_spawn_hands_init_agent_the_swarm_lane_and_task(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTIHOOKS_SWARM", raising=False)
    seen = {}

    def run(argv, **kwargs):
        seen["env"] = kwargs.get("env")
        return SimpleNamespace(returncode=0, stdout="status=started\nroute_status=routed\n", stderr="")

    runtime = HerdrRuntime(home=tmp_path, run=run, choose=lambda *_: ("claude", "open"))
    config = SimpleNamespace(slug="swarm-buildout", repo=str(tmp_path), compact_limit=0)
    runtime.spawn(config, "eng", "swarm-buildout-eng-4", {"id": "t4", "title": "x"})
    assert seen["env"]["AGENTIHOOKS_SWARM"] == "swarm-buildout"
    assert seen["env"]["AGENTIHOOKS_SWARM_LANE"] == "eng"
    assert seen["env"]["AGENTIHOOKS_SWARM_TASK"] == "t4"


def test_spawn_records_the_model_and_effort_init_agent_launched_with(tmp_path):
    out = "status=started\nroute_status=routed\npane_id=w1:p2\naccount=a\nmodel=opus\neffort=high\n"
    runtime = HerdrRuntime(
        home=tmp_path,
        run=lambda argv, **kw: SimpleNamespace(returncode=0, stdout=out, stderr=""),
        choose=lambda *_: ("claude", "open"),
    )
    config = SimpleNamespace(slug="sw", repo=str(tmp_path), compact_limit=0)
    placed = runtime.spawn(config, "eng", "sw-eng-1", {"id": "t1", "title": "x"})
    assert (placed.model, placed.effort) == ("opus", "high")


def _spawn_env(tmp_path, monkeypatch, **config):
    monkeypatch.delenv("AGENTIHOOKS_COMPACT_LIMIT", raising=False)
    seen = {}

    def run(argv, **kwargs):
        seen["env"] = kwargs.get("env")
        return SimpleNamespace(returncode=0, stdout="status=started\nroute_status=routed\n", stderr="")

    runtime = HerdrRuntime(home=tmp_path, run=run, choose=lambda *_: ("claude", "open"))
    runtime.spawn(
        SimpleNamespace(slug="sw", repo=str(tmp_path), **config), "eng", "sw-eng-1", {"id": "t1", "title": "x"}
    )
    return seen["env"]


def test_spawn_hands_init_agent_the_swarm_compact_limit(tmp_path, monkeypatch):
    assert _spawn_env(tmp_path, monkeypatch, compact_limit=40)["AGENTIHOOKS_COMPACT_LIMIT"] == "40"


def test_spawn_without_a_swarm_compact_limit_leaves_the_default(tmp_path, monkeypatch):
    assert "AGENTIHOOKS_COMPACT_LIMIT" not in _spawn_env(tmp_path, monkeypatch, compact_limit=0)
