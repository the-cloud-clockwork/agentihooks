from types import SimpleNamespace

from scripts.swarm.runtime import HerdrRuntime


def test_spawn_hands_init_agent_the_swarm_lane_and_task(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTIHOOKS_SWARM", raising=False)
    seen = {}

    def run(argv, **kwargs):
        seen["env"] = kwargs.get("env")
        return SimpleNamespace(returncode=0, stdout="status=started\nroute_status=routed\n", stderr="")

    runtime = HerdrRuntime(home=tmp_path, run=run, choose=lambda *_: ("claude", "open"))
    config = SimpleNamespace(slug="swarm-buildout", repo=str(tmp_path), compact_limit=0, lanes={}, autonomy="delegate")
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
    config = SimpleNamespace(slug="sw", repo=str(tmp_path), compact_limit=0, lanes={}, autonomy="delegate")
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
        SimpleNamespace(slug="sw", repo=str(tmp_path), lanes={}, **{"autonomy": "delegate", **config}),
        "eng",
        "sw-eng-1",
        {"id": "t1", "title": "x"},
    )
    return seen["env"]


def test_spawn_hands_init_agent_the_swarm_compact_limit(tmp_path, monkeypatch):
    assert _spawn_env(tmp_path, monkeypatch, compact_limit=40)["AGENTIHOOKS_COMPACT_LIMIT"] == "40"


def test_spawn_without_a_swarm_compact_limit_leaves_the_default(tmp_path, monkeypatch):
    assert "AGENTIHOOKS_COMPACT_LIMIT" not in _spawn_env(tmp_path, monkeypatch, compact_limit=0)


def _spawn_seen(tmp_path, lanes, lane="eng"):
    seen = {}

    def run(argv, **kwargs):
        seen["argv"] = argv
        return SimpleNamespace(returncode=0, stdout="status=started\nroute_status=routed\n", stderr="")

    def choose(requested, environ):
        seen["requested"] = requested
        return requested or "claude", "requested" if requested else "priority"

    runtime = HerdrRuntime(home=tmp_path, run=run, choose=choose)
    config = SimpleNamespace(slug="sw", repo=str(tmp_path), compact_limit=0, lanes=lanes, autonomy="delegate")
    runtime.spawn(config, lane, "sw-eng-1", {"id": "t1", "title": "x"})
    return seen


def _passed(argv):
    return argv[argv.index("--") + 1 :] if "--" in argv else []


def test_spawn_passes_a_codex_lane_agent_model_and_effort_to_init_agent(tmp_path):
    seen = _spawn_seen(tmp_path, {"eng": {"agent": "codex", "model": "gpt-6.1-sol", "effort": "xhigh"}})
    assert seen["requested"] == "codex"
    assert seen["argv"][seen["argv"].index("--agent") + 1] == "codex"
    assert _passed(seen["argv"]) == ["-m", "gpt-6.1-sol", "-c", 'model_reasoning_effort="xhigh"']


def test_spawn_passes_a_claude_lane_model_and_effort_to_init_agent(tmp_path):
    seen = _spawn_seen(tmp_path, {"eng": {"agent": "claude", "model": "sonnet", "effort": "max"}})
    assert seen["requested"] == "claude"
    assert _passed(seen["argv"]) == ["--model", "sonnet", "--effort", "max"]


def test_an_auto_lane_falls_back_to_the_automatic_choice(tmp_path):
    seen = _spawn_seen(tmp_path, {"eng": {"agent": "auto", "model": "auto", "effort": "auto"}})
    assert seen["requested"] == "" and "--" not in seen["argv"]


def test_a_lane_without_an_entry_falls_back_to_the_automatic_choice(tmp_path):
    seen = _spawn_seen(tmp_path, {"eng": {"agent": "codex"}}, lane="ci")
    assert seen["requested"] == "" and "--" not in seen["argv"]


def test_a_lane_model_goes_to_the_automatically_chosen_agent(tmp_path):
    seen = _spawn_seen(tmp_path, {"eng": {"agent": "auto", "model": "sonnet", "effort": "auto"}})
    assert seen["requested"] == "" and _passed(seen["argv"]) == ["--model", "sonnet"]


def test_the_lane_role_replaces_the_default_role_in_the_prompt(tmp_path):
    _spawn_seen(tmp_path, {"eng": {"role": "a reviewer who only reads"}})
    text = (tmp_path / "sw" / "prompts" / "sw-eng-1.md").read_text()
    assert text.startswith("You are sw-eng-1, a reviewer who only reads in swarm sw,")


def test_an_auto_work_lane_spawn_asks_for_the_codex_share_with_the_swarm_settings(tmp_path, monkeypatch):
    from scripts import agent_choice

    seen = {}

    def shared(requested, environ, spawns, share, min_week_left, choose):
        seen.update(requested=requested, spawns=spawns, share=share, min_week_left=min_week_left)
        return "codex", "codex share 0/2 below 30%"

    def run(argv, **kwargs):
        seen["argv"] = argv
        return SimpleNamespace(returncode=0, stdout="status=started\nroute_status=routed\n", stderr="")

    monkeypatch.setattr(agent_choice, "choose_shared", shared)
    monkeypatch.delenv("AGENTIHOOKS_SWARM_CODEX_SHARE", raising=False)
    monkeypatch.setenv("AGENTIHOOKS_SWARM_CODEX_MIN_WEEK_LEFT", "7")
    runtime = HerdrRuntime(home=tmp_path, run=run, choose=lambda *_: ("claude", "priority"))
    config = SimpleNamespace(
        slug="sw",
        repo=str(tmp_path),
        compact_limit=0,
        lanes={"eng": {"agent": "auto"}},
        autonomy="delegate",
        codex_share=None,
        codex_min_week_left=None,
    )
    runtime.spawn(config, "eng", "sw-eng-1", {"id": "t1", "title": "x"}, spawns={"claude": 2})
    assert seen["requested"] == "" and seen["spawns"] == {"claude": 2}
    assert (seen["share"], seen["min_week_left"]) == (30, 7)
    assert seen["argv"][seen["argv"].index("--agent") + 1] == "codex"
