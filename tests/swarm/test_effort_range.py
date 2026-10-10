from types import SimpleNamespace

import pytest

from hooks.classifier import Answer, DecisionResult
from scripts import init_agent
from scripts.swarm import cli, effort_range, model_pick
from scripts.swarm.keyspace import ROOT as KEY_ROOT
from scripts.swarm.store import SwarmConfig, SwarmError
from tests.swarm.test_cli import env, run  # noqa: F401
from tests.swarm.test_runtime import _launched, _passed, _resuming

pytestmark = pytest.mark.xdist_group("fakeredis")

TASK = {"id": "t1", "title": "x"}


from tests.swarm.profile_fixture import validated


def _answer(score):
    return lambda *a, **kw: DecisionResult({"effort": Answer("score", score=score, confidence=0.95)}, "luna")


@pytest.mark.parametrize(
    "harness,score,floor,launch",
    [
        ("claude", 3, "low", ["--model", "opus", "--effort", "high"]),
        ("claude", 0, "low", ["--model", "opus", "--effort", "medium"]),
        ("codex", 3, "low", ["-m", "gpt-6.1-sol", "-c", 'model_reasoning_effort="high"']),
        ("codex", 0, "low", ["-m", "gpt-6.1-sol", "-c", 'model_reasoning_effort="medium"']),
    ],
)
def test_a_classifier_answer_is_clamped_into_the_default_range(tmp_path, monkeypatch, harness, score, floor, launch):
    monkeypatch.setattr(model_pick, "decide", _answer(score))
    launch_env = {f"AGENTIHOOKS_{harness.upper()}_EFFORT": floor}
    assert _launched(tmp_path, monkeypatch, "eng", TASK, harness=harness, env=launch_env) == launch


@pytest.mark.parametrize(
    "score,effort",
    [(3, "max"), (0, "low")],
)
def test_a_range_widened_to_low_and_max_lets_the_classifier_answer_through(tmp_path, monkeypatch, score, effort):
    monkeypatch.setattr(model_pick, "decide", _answer(score))
    monkeypatch.setattr(effort_range, "DEFAULT", ("low", "max"))
    launch_env = {"AGENTIHOOKS_CLAUDE_EFFORT": "low"}
    assert _launched(tmp_path, monkeypatch, "eng", TASK, env=launch_env) == ["--model", "opus", "--effort", effort]


@pytest.mark.parametrize("lane", ["eng", "ci", "plan", "master"])
def test_a_lane_or_environment_effort_outside_the_range_launches_at_its_edge(tmp_path, monkeypatch, lane):
    lanes = {lane: {"agent": "claude", "model": "auto", "effort": "max"}}
    task = {"id": "master", "peer": ""} if lane == "master" else TASK
    launch_env = {"AGENTIHOOKS_CLAUDE_EFFORT": "max"}
    assert _launched(tmp_path, monkeypatch, lane, task, lanes, env=launch_env)[-2:] == ["--effort", "high"]


def test_spawn_hands_init_agent_the_swarm_effort_range(tmp_path):
    seen = {}

    def launch(argv, **kwargs):
        seen["env"] = kwargs["env"]
        return SimpleNamespace(returncode=0, stdout=validated(argv, "status=started\nroute_status=routed\n"), stderr="")

    from scripts.swarm.runtime import HerdrRuntime

    runtime = HerdrRuntime(home=tmp_path, run=launch, choose=lambda *_: ("claude", "open"))
    config = SimpleNamespace(
        slug="sw",
        repo=str(tmp_path),
        code="a1b2c3",
        compact_limit=0,
        lanes={},
        autonomy="delegate",
        effort_min="low",
        effort_max="medium",
    )
    runtime.spawn(config, "eng", "engineer@a1b2c3-0001", TASK)
    assert seen["env"]["AGENTIHOOKS_SWARM_EFFORT_RANGE"] == "low:medium"


def test_a_resumed_agent_relaunches_inside_the_range(tmp_path):
    from dataclasses import replace

    runtime, config, agent, seen = _resuming(tmp_path, "c0ffee")
    config.lanes = {"eng": {"model": "fable", "effort": "max"}}
    runtime.resume(config, replace(agent, model="sonnet", effort="medium"), "you were restored")
    assert _passed(seen["runs"][0]) == ["--route", "a1", "--model", "sonnet", "--effort", "medium"]


def test_spawn_and_resume_take_the_swarm_range_not_the_default(tmp_path):
    from dataclasses import replace

    from scripts.swarm.runtime import HerdrRuntime

    seen = []

    def launch(argv, **kwargs):
        seen.append(argv)
        return SimpleNamespace(returncode=0, stdout=validated(argv, "status=started\nroute_status=routed\n"), stderr="")

    runtime = HerdrRuntime(home=tmp_path, run=launch, choose=lambda *_: ("claude", "open"))
    config = SimpleNamespace(
        slug="sw",
        repo=str(tmp_path),
        code="a1b2c3",
        compact_limit=0,
        lanes={"eng": {"model": "fable", "effort": "max"}},
        autonomy="delegate",
        effort_min="low",
        effort_max="max",
    )
    runtime.spawn(config, "eng", "engineer@a1b2c3-0001", TASK)
    assert _passed(seen[-1]) == ["--model", "fable", "--effort", "max"]
    resuming, _, agent, resumed = _resuming(tmp_path, "c0ffee")
    resuming.resume(config, replace(agent, model="sonnet", effort="low"), "you were restored")
    assert _passed(resumed["runs"][0]) == ["--route", "a1", "--model", "sonnet", "--effort", "low"]


@pytest.mark.parametrize(
    "agent,args,environ,expected",
    [
        ("claude", ["--effort", "max"], {"AGENTIHOOKS_SWARM_LANE": "eng"}, "high"),
        ("claude", ["--effort=low"], {"AGENTIHOOKS_SWARM_LANE": "eng"}, "medium"),
        ("claude", [], {"AGENTIHOOKS_SWARM_LANE": "eng", "AGENTIHOOKS_CLAUDE_EFFORT": "max"}, "high"),
        ("codex", ["-c", 'model_reasoning_effort="xhigh"'], {"AGENTIHOOKS_SWARM_LANE": "ci"}, "high"),
        (
            "claude",
            ["--effort", "max"],
            {"AGENTIHOOKS_SWARM_LANE": "eng", "AGENTIHOOKS_SWARM_EFFORT_RANGE": "low:max"},
            "max",
        ),
        ("claude", ["--effort", "max"], {}, "max"),
    ],
)
def test_init_agent_clamps_a_swarm_lane_launch_into_the_range(
    tmp_path, capsys, monkeypatch, agent, args, environ, expected
):
    monkeypatch.setattr(init_agent, "_launch_command", lambda launcher, *a: ("linux", ["/usr/bin/term", str(launcher)]))
    rc = init_agent.main(
        ["--dir", str(tmp_path), "--agent", agent, "--host", "native", "--dry-run", "--", *args],
        {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin", "AGENTIHOOKS_SWARM_SPAWN": "1", **environ},
    )
    assert rc == 0
    fields = dict(line.split("=", 1) for line in capsys.readouterr().out.splitlines() if "=" in line)
    assert fields["effort"] == expected
    assert fields["claude_args"].count("effort") <= 1


def test_set_refuses_a_lane_effort_outside_the_range_and_names_it(env, capsys):  # noqa: F811
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    capsys.readouterr()
    assert run("sw", "set", "eng-effort=max") == 1
    assert "medium to high" in capsys.readouterr().err
    assert run("sw", "set", "ci-effort=xhigh") == 1
    assert run("sw", "set", "eng-effort=low") == 1
    assert run("sw", "set", "eng-effort=high") == 0
    assert store.config("sw").lanes["eng"]["effort"] == "high"


def test_the_range_defaults_to_medium_and_high_and_widens_to_let_both_through(env, capsys):  # noqa: F811
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    config = store.config("sw")
    assert (config.effort_min, config.effort_max) == ("medium", "high")
    assert run("sw", "set", "effort-min=low", "effort-max=max") == 0
    assert run("sw", "set", "eng-effort=max", "ci-effort=low") == 0
    config = store.config("sw")
    assert (config.effort_min, config.effort_max) == ("low", "max")
    assert (config.lanes["eng"]["effort"], config.lanes["ci"]["effort"]) == ("max", "low")
    capsys.readouterr()
    run("sw", "status")
    assert "effort low to max" in capsys.readouterr().out.splitlines()[0]


@pytest.mark.parametrize("pairs", [["effort-min=high", "effort-max=medium"], ["effort-max=huge"], ["effort-min=7"]])
def test_set_refuses_an_unordered_or_unknown_range(env, pairs):  # noqa: F811
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    assert run("sw", "set", *pairs) == 1
    config = store.config("sw")
    assert (config.effort_min, config.effort_max) == ("medium", "high")


@pytest.mark.parametrize(
    "harness,effort,named",
    [("codex", "max", "xhigh"), ("claude", "xhigh", "max"), ("codex", "medium", "medium"), ("codex", "turbo", "turbo")],
)
def test_an_effort_is_named_for_a_harness_by_its_rank(harness, effort, named):
    assert effort_range.named(harness, effort) == named


def test_a_codex_name_sets_the_range_on_the_shared_scale(env):  # noqa: F811
    store, _, _ = env
    assert cli.EFFORT_KEYS == {"effort-min": "effort_min", "effort-max": "effort_max"}
    run("sw", "create", "--repo", "/repo")
    assert run("sw", "set", "effort-max=xhigh") == 0
    assert store.config("sw").effort_max == "max"


def test_bare_pairs_route_to_set_by_their_key_alone(env):  # noqa: F811
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    assert run("sw", "eng-role=a=b", "autonomy=full") == 0
    config = store.config("sw")
    assert (config.autonomy, config.lanes["eng"]["role"]) == ("full", "a=b")
    assert run("sw", "autonomy=assist") == 0
    assert store.config("sw").autonomy == "assist"
    with pytest.raises(SystemExit):
        run("sw")


@pytest.fixture
def saved():
    import fakeredis

    from scripts.swarm.store import RedisStore

    return RedisStore(fakeredis.FakeRedis(decode_responses=True))


def test_a_swarm_stored_before_the_range_reads_the_default_and_keeps_its_lanes_editable(saved):
    saved.redis.hset(
        f"{KEY_ROOT}:swarm:old:config",
        mapping={
            "slug": "old",
            "repo": "/r",
            "max_eng": 1,
            "max_ci": 0,
            "state": "paused",
            "lanes": '{"eng": {"effort": "max"}}',
        },
    )
    config = saved.config("old")
    assert (config.effort_min, config.effort_max) == ("medium", "high")
    assert saved.update("old", max_eng=3).max_eng == 3
    with pytest.raises(SwarmError, match="lane eng effort max is outside the swarm effort range medium to high"):
        saved.update("old", lanes={"eng": {"effort": "max"}})


def test_store_update_checks_and_normalises_the_range(saved):
    saved.create(SwarmConfig("sw", "/r", 1, 0))
    config = saved.update("sw", effort_min="low", effort_max="xhigh")
    assert (config.effort_min, config.effort_max) == ("low", "max")
    assert saved.config("sw").effort_max == "max"
    assert saved.update("sw", effort_min="xhigh").effort_min == "max"
    config = saved.update("sw", effort_min="low")
    with pytest.raises(SwarmError, match="effort-min high is above effort-max medium"):
        saved.update("sw", effort_min="high", effort_max="medium")
    with pytest.raises(SwarmError, match="one of low, medium, high, max"):
        saved.update("sw", effort_max="huge")
    assert (saved.config("sw").effort_min, saved.config("sw").effort_max) == ("low", "max")


def test_store_create_refuses_an_out_of_range_lane_and_stores_nothing(saved):
    with pytest.raises(SwarmError, match="lane ci effort low is outside the swarm effort range medium to high"):
        saved.create(SwarmConfig("sw", "/r", 1, 0, lanes={"ci": {"effort": "low"}}))
    assert saved.slugs() == [] and not saved.redis.exists(f"{KEY_ROOT}:swarm:sw:config")


def test_create_refuses_a_template_lane_effort_outside_the_range(env, tmp_path, monkeypatch, capsys):  # noqa: F811
    import json

    store, _, _ = env
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    (tmp_path / "swarm-templates").mkdir()
    lanes = {"eng": {"effort": "max"}}
    (tmp_path / "swarm-templates" / "hot.json").write_text(json.dumps({"name": "hot", "lanes": lanes}))
    assert run("sw", "create", "--repo", "/repo", "--template", "hot") == 1
    assert "outside the swarm effort range medium to high" in capsys.readouterr().err
    assert store.slugs() == []


def test_set_reports_the_range_takes_bare_pairs_and_a_one_level_range(env, capsys):  # noqa: F811
    import json

    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    capsys.readouterr()
    assert run("sw", "effort-min=high", "max-eng-agents=3") == 0
    out = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert (out["effort_min"], out["effort_max"], out["max_eng"]) == ("high", "high", 3)
    assert run("sw", "effort-max=max") == 0
    assert store.config("sw").effort_max == "max"


def test_set_names_every_key_it_takes_and_every_level(env, capsys):  # noqa: F811
    run("sw", "create", "--repo", "/repo")
    capsys.readouterr()
    assert run("sw", "set", "colour=red") == 1
    err = capsys.readouterr().err
    assert "set takes max-eng-agents, max-ci-agents, " in err
    assert (
        "autonomy=manual|assist|delegate|full, effort-min=E, effort-max=E, scaling=auto|manual, "
        "load-high=N, load-low=N, memory-per-agent=MB or eng-role, "
    ) in err
    assert run("sw", "set", "effort-min=huge") == 1
    assert "one of low, medium, high, max, Codex xhigh standing for max" in capsys.readouterr().err


def test_a_lane_without_an_effort_passes_the_range():
    assert effort_range.refusal(effort_range.DEFAULT, {"eng": {}, "ci": {"effort": "auto"}}) is None


@pytest.mark.parametrize(
    "agent,args,expected",
    [
        (
            "claude",
            ["--effort", "max", "--model", "opus", "--resume", "c0"],
            ["--model", "opus", "--resume", "c0", "--effort", "high"],
        ),
        ("claude", ["--name", "x", "--effort=low"], ["--name", "x", "--effort", "medium"]),
        (
            "codex",
            ["-c", 'model_reasoning_effort="xhigh"', "-c", "other=1", "-m", "g"],
            ["-c", "other=1", "-m", "g", "-c", 'model_reasoning_effort="high"'],
        ),
        (
            "codex",
            ["model_reasoning_effort=low", "resume", "-c"],
            ["resume", "-c", "-c", 'model_reasoning_effort="medium"'],
        ),
    ],
)
def test_launch_args_keep_every_other_arg_and_carry_one_clamped_effort(agent, args, expected):
    environ = {"AGENTIHOOKS_SWARM_LANE": "eng"}
    assert effort_range.launch_args(agent, args, environ) == expected
    assert effort_range.launch_args(agent, args, {}) == args
