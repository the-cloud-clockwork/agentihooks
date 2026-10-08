import json
import subprocess
from dataclasses import replace
from types import SimpleNamespace

import pytest

from hooks.context import profile_chain
from scripts.swarm import cli, overlays, profile_choice, runtime, status, store, tick
from tests.swarm.profile_fixture import validated
from tests.swarm.test_cli import env  # noqa: F401

pytestmark = pytest.mark.xdist_group("fakeredis")


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    from scripts.targets._common import _install_module

    roles = tmp_path / "roles"
    monkeypatch.setattr(profile_chain, "PACKAGE_ROLES", roles)
    for role in ("engineer", "qa"):
        (roles / role).mkdir(parents=True)
    root = tmp_path / "bundle"
    manifests = {
        "tuner": "kind: overlay\nwears: [engineer]\n",
        "trader": "kind: overlay\nwears: [engineer, qa]\n",
        "scout": "kind: overlay\nwears: [engineer]\n",
        "extra": "kind: overlay\nwears: [engineer]\n",
        "planning": "kind: overlay\nwears: [planner]\n",
        "broken": "kind: overlay\nwears: engineer\n",
        "garbled": "kind: overlay\nwears: [engineer\n",
        "scalar": "just text\n",
        "engineer": "extends: package:engineer\n",
    }
    for name, text in manifests.items():
        (root / "profiles" / name).mkdir(parents=True)
        (root / "profiles" / name / "profile.yml").write_text(text)
    chains = {"engineer": [("engineer", root / "profiles" / "engineer"), ("package:engineer", roles / "engineer")]}
    installer = _install_module()
    monkeypatch.setattr(installer, "_get_bundle_path", lambda: root)
    monkeypatch.setattr(installer, "_resolve_profile_chain", lambda name: chains.get(name, []))
    monkeypatch.setattr(
        installer, "_resolve_profile_dir", lambda name: d if (d := root / "profiles" / name).is_dir() else None
    )
    return root


OFFERED = [{"name": "tuner", "wears": ["engineer"]}, {"name": "trader", "wears": ["engineer", "qa"]}]


def test_setting_stores_a_role_default_and_an_empty_value_clears_it():
    assert overlays.setting("overlays-engineer", "tuner, trader,tuner", {}, OFFERED) == {
        "engineer": ["tuner", "trader"]
    }
    current = {"engineer": ["tuner"], "qa": ["trader"]}
    assert overlays.setting("overlays-engineer", "", current, OFFERED) == {"qa": ["trader"]}
    assert current == {"engineer": ["tuner"], "qa": ["trader"]}


@pytest.mark.parametrize(
    "key,value,message",
    [
        (
            "overlays-frontend",
            "tuner",
            "overlays are set per base role: overlays-master, overlays-engineer, overlays-planner, overlays-qa, "
            "overlays-cicd",
        ),
        ("overlays-engineer", "a,b,c,d", "a role wears at most 3 overlays; 4 were set: a, b, c, d"),
        ("overlays-engineer", "tuner,nope", "overlay nope not found"),
        ("overlays-qa", "trader,tuner", "overlay tuner does not wear the qa role"),
    ],
)
def test_setting_refuses_an_unknown_role_overlay_or_more_than_three(key, value, message):
    with pytest.raises(ValueError) as refused:
        overlays.setting(key, value, {}, OFFERED)
    assert str(refused.value) == message


def test_the_task_list_wins_over_the_role_default(bundle):
    defaults = {"engineer": ["tuner"], "qa": ["trader"]}
    assert overlays.chosen("engineer", {"id": "t1"}, defaults) == ("tuner",)
    assert overlays.chosen("engineer", {"id": "t1", "overlays": ["trader", "scout"]}, defaults) == ("scout", "trader")
    assert overlays.chosen("engineer", {"id": "t1", "overlays": []}, defaults) == ()
    assert overlays.chosen("engineer", {"id": "t1"}, {"qa": ["trader"]}) == ()
    assert overlays.chosen("engineer", {"id": "t1"}, {}) == ()


@pytest.mark.parametrize(
    "task,message",
    [
        ({"overlays": ["planning"]}, "overlay planning does not wear the engineer role"),
        (
            {"overlays": ["tuner", "trader", "scout", "extra"]},
            "an agent wears at most 3 overlays; 4 were chosen: tuner, trader, scout, extra",
        ),
    ],
)
def test_choice_refuses_an_overlay_the_role_cannot_wear(bundle, monkeypatch, task, message):
    monkeypatch.setattr(profile_choice, "installed", lambda name: True)
    with pytest.raises(profile_choice.ProfileUnresolved) as refused:
        profile_choice.choose("sw", "eng", {}, {"id": "t1", "profile": "engineer", **task}, {}, {})
    assert str(refused.value) == f"task t1 overlays are refused: {message}"


def test_profile_choice_returns_the_overlays_the_agent_wears(bundle, monkeypatch):
    monkeypatch.setattr(profile_choice, "installed", lambda name: True)
    task = {"id": "t1", "profile": "engineer"}
    decision = profile_choice.choose("sw", "eng", {}, task, {}, {"engineer": ["tuner", "trader"]})
    assert decision.overlays == ("trader", "tuner")
    assert decision.record()["overlays"] == ["trader", "tuner"]
    assert profile_choice.choose("sw", "eng", {}, task, {}).overlays == ()


def test_available_lists_each_bundle_overlay_with_the_roles_it_wears(bundle):
    assert overlays.available() == [
        {"name": "extra", "wears": ["engineer"]},
        {"name": "planning", "wears": ["planner"]},
        {"name": "scout", "wears": ["engineer"]},
        {"name": "trader", "wears": ["engineer", "qa"]},
        {"name": "tuner", "wears": ["engineer"]},
    ]


def test_available_is_empty_without_a_bundle(monkeypatch):
    from scripts.targets._common import _install_module

    monkeypatch.setattr(_install_module(), "_get_bundle_path", lambda: None)
    assert overlays.available() == []
    assert overlays.revision() == ""


def test_setting_takes_three_overlays():
    offered = [*OFFERED, {"name": "scout", "wears": ["engineer"]}]
    assert overlays.setting("overlays-engineer", "tuner,trader,scout", {}, offered) == {
        "engineer": ["tuner", "trader", "scout"]
    }


def test_revision_waits_ten_seconds_for_git(bundle, monkeypatch):
    seen = {}

    def run(argv, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="abc\n")

    monkeypatch.setattr(overlays.subprocess, "run", run)
    assert overlays.revision() == "abc"
    assert seen == {"capture_output": True, "text": True, "timeout": 10}


def test_revision_is_the_bundle_head_commit(bundle):
    assert overlays.revision() == ""
    subprocess.run(["git", "init", "-q", str(bundle)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(bundle),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "x",
        ],
        check=True,
    )
    head = subprocess.run(["git", "-C", str(bundle), "rev-parse", "HEAD"], capture_output=True, text=True)
    assert overlays.revision() == head.stdout.strip()


@pytest.fixture
def swarm(env):  # noqa: F811
    saved, _, _ = env
    cli.main(["sw", "create", "--repo", "/repo"])
    return saved


def test_swarm_set_stores_overlays_per_base_role(bundle, swarm, capsys):
    assert cli.main(["sw", "set", "overlays-engineer=tuner,trader", "overlays-qa=trader"]) == 0
    assert swarm.config("sw").overlays == {"engineer": ["tuner", "trader"], "qa": ["trader"]}
    assert json.loads(capsys.readouterr().out)["overlays"] == {"engineer": ["tuner", "trader"], "qa": ["trader"]}
    assert cli.main(["sw", "set", "overlays-engineer="]) == 0
    assert swarm.config("sw").overlays == {"qa": ["trader"]}


def test_swarm_set_refuses_a_fourth_overlay_and_keeps_the_config(bundle, swarm, capsys):
    cli.main(["sw", "set", "overlays-engineer=tuner"])
    capsys.readouterr()
    assert cli.main(["sw", "set", "overlays-engineer=a,b,c,d"]) == 1
    assert swarm.config("sw").overlays == {"engineer": ["tuner"]}
    assert capsys.readouterr().err.strip().endswith("a role wears at most 3 overlays; 4 were set: a, b, c, d")


def test_swarm_set_refuses_an_overlay_the_bundle_does_not_offer_the_role(bundle, swarm, capsys):
    assert cli.main(["sw", "set", "overlays-engineer=planning"]) == 1
    assert swarm.config("sw").overlays == {}
    assert capsys.readouterr().err.strip().endswith("overlay planning does not wear the engineer role")


def test_a_swarm_without_overlays_reads_an_empty_map(swarm):
    swarm.redis.hdel(swarm.key("sw", "config"), "overlays")
    assert swarm.config("sw").overlays == {}


def _spawned(tmp_path, monkeypatch, task, config_overlays, saved=None, revision="abc123"):
    seen = {}
    monkeypatch.setattr(overlays, "revision", lambda: revision)
    monkeypatch.setattr(profile_choice, "installed", lambda name: True)

    def run(argv, **kwargs):
        seen["argv"] = argv
        return SimpleNamespace(returncode=0, stdout=validated(argv, "status=started\nroute_status=routed\n"), stderr="")

    rt = runtime.HerdrRuntime(home=tmp_path, run=run, choose=lambda *_: ("claude", "open"))
    config = store.SwarmConfig("sw", str(tmp_path), 1, 0, code="a1b2c3", overlays=config_overlays)
    placed = rt.spawn(config, "eng", "engineer@a1b2c3-0001", {"id": "t1", "title": "x", "profile": "engineer", **task})
    return placed, seen["argv"]


def _passed_overlays(argv):
    return [argv[i + 1] for i, arg in enumerate(argv) if arg == "--overlay"]


def test_spawn_passes_the_role_overlays_and_records_them_with_the_bundle_revision(bundle, tmp_path, monkeypatch):
    placed, argv = _spawned(tmp_path, monkeypatch, {}, {"engineer": ["tuner", "trader"]})
    assert _passed_overlays(argv[: argv.index("--")]) == ["trader", "tuner"]
    assert placed.overlays == ["trader", "tuner"]
    assert placed.profile_decision["overlays"] == ["trader", "tuner"]
    assert placed.profile_decision["bundle_revision"] == "abc123"
    record = tick.placed_record(store.AgentRecord("engineer@a1b2c3-0001", "eng", "t1"), placed)
    assert record.overlays == ["trader", "tuner"]


def test_spawn_wears_the_task_overlays_over_the_role_default(bundle, tmp_path, monkeypatch):
    placed, argv = _spawned(tmp_path, monkeypatch, {"overlays": ["scout"]}, {"engineer": ["tuner"]})
    assert _passed_overlays(argv) == ["scout"]
    assert placed.overlays == ["scout"]


def test_spawn_without_overlays_passes_none(bundle, tmp_path, monkeypatch):
    placed, argv = _spawned(tmp_path, monkeypatch, {}, {})
    assert _passed_overlays(argv) == []
    assert placed.overlays == []
    assert placed.profile_decision["bundle_revision"] == "abc123"


def test_a_relaunch_wears_the_overlays_its_launch_recorded(bundle, tmp_path, monkeypatch):
    saved = {
        "profile": "engineer",
        "harness": "claude",
        "model": "opus",
        "effort": "high",
        "overlays": ["scout"],
        "bundle_revision": "def456",
    }
    placed, argv = _spawned(tmp_path, monkeypatch, {"launch_assignment": saved}, {"engineer": ["tuner"]})
    assert _passed_overlays(argv) == ["scout"]
    assert placed.overlays == ["scout"]
    assert placed.profile_decision["bundle_revision"] == "def456"


def test_a_handoff_keeps_the_bundle_revision_its_launch_recorded(bundle, tmp_path, monkeypatch):
    launch = {
        "profile": "engineer",
        "harness": "claude",
        "model": "opus",
        "effort": "high",
        "overlays": ["scout"],
        "profile_decision": {"bundle_revision": "fed789"},
    }
    task = {"profile": "", "handoff_envelope": {"launch": launch}}
    placed, argv = _spawned(tmp_path, monkeypatch, task, {"engineer": ["tuner"]})
    assert _passed_overlays(argv) == ["scout"]
    assert placed.profile_decision["bundle_revision"] == "fed789"


def test_a_relaunch_without_a_recorded_revision_pins_the_current_one(bundle, tmp_path, monkeypatch):
    saved = {"profile": "engineer", "harness": "claude", "model": "opus", "effort": "high"}
    placed, _ = _spawned(tmp_path, monkeypatch, {"launch_assignment": saved}, {})
    assert placed.profile_decision["bundle_revision"] == "abc123"


def test_the_relaunch_assignment_carries_the_agent_overlays():
    from scripts.swarm import live_binding

    agent = store.AgentRecord(
        "engineer@a1b2c3-0001",
        "eng",
        "t1",
        profile="engineer",
        overlays=["tuner"],
        profile_decision={"bundle_revision": "abc123"},
    )
    config = store.SwarmConfig("sw", "/repo", 1, 0)
    saved = live_binding.relaunch_assignment(agent, {}, config)
    assert (saved["overlays"], saved["bundle_revision"]) == (["tuner"], "abc123")
    bare = store.AgentRecord("engineer@a1b2c3-0002", "eng", "t1", profile="engineer")
    assert live_binding.relaunch_assignment(bare, {}, config)["bundle_revision"] == ""


def test_resume_wears_the_overlays_the_agent_was_launched_with(tmp_path, monkeypatch):
    seen = {}

    def run(argv, **kwargs):
        seen["argv"] = argv
        return SimpleNamespace(
            returncode=0, stdout=validated(argv, "status=started\nroute_status=routed\npane_id=w1:p1\n"), stderr=""
        )

    rt = runtime.HerdrRuntime(home=tmp_path, run=run, choose=lambda *_: ("claude", "open"))
    rt.conversations = lambda: {"w1:p1": "conv-1"}
    agent = store.AgentRecord(
        "engineer@a1b2c3-0001",
        "eng",
        "t1",
        harness="claude",
        profile="engineer",
        conversation_id="conv-1",
        overlays=["tuner"],
    )
    agent = replace(agent, profile_decision={"bundle_revision": "abc123"})
    placed = rt.resume(store.SwarmConfig("sw", str(tmp_path), 1, 0, code="a1b2c3"), agent, "resume")
    assert _passed_overlays(seen["argv"]) == ["tuner"]
    assert placed.overlays == ["tuner"]


def test_status_carries_agent_overlays_and_the_overlays_a_bundle_offers(bundle, swarm, monkeypatch):
    monkeypatch.setattr(status, "page_quota", lambda: [])
    swarm.put_agent("sw", store.AgentRecord("engineer@a1b2c3-0001", "eng", "t1", overlays=["tuner"]))
    report = status.status_report(swarm, "sw", {"tasks": []})
    assert report["agents"][0]["overlays"] == ["tuner"]
    assert report["config"]["overlays"] == {}
    assert {"name": "trader", "wears": ["engineer", "qa"]} in report["overlays_available"]
    assert [row["name"] for row in report["overlays_available"]] == ["extra", "planning", "scout", "trader", "tuner"]


def _passed_revision(argv):
    head = argv[: argv.index("--")]
    return [head[i + 1] for i, arg in enumerate(head) if arg == "--bundle-revision"]


def test_an_overlay_launch_pins_its_render_to_the_recorded_bundle_revision(bundle, tmp_path, monkeypatch):
    _, argv = _spawned(tmp_path, monkeypatch, {}, {"engineer": ["tuner"]})
    assert _passed_revision(argv) == ["abc123"]


def test_an_overlay_relaunch_pins_its_render_to_the_revision_its_launch_recorded(bundle, tmp_path, monkeypatch):
    saved = {"profile": "engineer", "harness": "claude", "model": "opus", "effort": "high"}
    saved |= {"overlays": ["scout"], "bundle_revision": "def456"}
    _, argv = _spawned(tmp_path, monkeypatch, {"launch_assignment": saved}, {})
    assert _passed_revision(argv) == ["def456"]


def test_a_launch_without_overlays_leaves_its_render_unpinned(bundle, tmp_path, monkeypatch):
    _, argv = _spawned(tmp_path, monkeypatch, {}, {})
    assert _passed_revision(argv) == []


def test_a_handoff_pins_its_render_to_the_revision_its_launch_recorded(bundle, tmp_path, monkeypatch):
    launch = {"profile": "engineer", "harness": "claude", "model": "opus", "effort": "high", "overlays": ["scout"]}
    task = {
        "profile": "",
        "handoff_envelope": {"launch": {**launch, "profile_decision": {"bundle_revision": "fed789"}}},
    }
    _, argv = _spawned(tmp_path, monkeypatch, task, {})
    assert _passed_revision(argv) == ["fed789"]


def test_a_handoff_on_a_task_profile_keeps_the_revision_its_launch_recorded(bundle, tmp_path, monkeypatch):
    launch = {
        "profile": "engineer",
        "harness": "claude",
        "model": "opus",
        "effort": "high",
        "bundle_revision": "fed789",
    }
    placed, argv = _spawned(tmp_path, monkeypatch, {"handoff_envelope": {"launch": launch}}, {"engineer": ["tuner"]})
    assert placed.profile_decision["bundle_revision"] == "fed789"
    assert _passed_revision(argv) == ["fed789"]


def test_a_handoff_onto_another_task_profile_pins_the_current_revision(bundle, tmp_path, monkeypatch):
    launch = {"profile": "qa", "harness": "claude", "model": "opus", "effort": "high", "bundle_revision": "fed789"}
    placed, _ = _spawned(tmp_path, monkeypatch, {"handoff_envelope": {"launch": launch}}, {"engineer": ["tuner"]})
    assert placed.profile_decision["bundle_revision"] == "abc123"


def test_an_overlay_launch_without_a_bundle_revision_is_refused(bundle, tmp_path, monkeypatch):
    with pytest.raises(tick.SpawnError) as refused:
        _spawned(tmp_path, monkeypatch, {}, {"engineer": ["tuner"]}, revision="")
    assert str(refused.value) == "an overlay launch needs the bundle commit it renders from, and none was recorded"


def _resumed(tmp_path, agent):
    seen = {}

    def run(argv, **kwargs):
        seen["argv"] = argv
        return SimpleNamespace(
            returncode=0, stdout=validated(argv, "status=started\nroute_status=routed\npane_id=w1:p1\n"), stderr=""
        )

    rt = runtime.HerdrRuntime(home=tmp_path, run=run, choose=lambda *_: ("claude", "open"))
    rt.conversations = lambda: {"w1:p1": "conv-1"}
    rt.resume(store.SwarmConfig("sw", str(tmp_path), 1, 0, code="a1b2c3"), agent, "resume")
    return seen["argv"]


def test_a_resumed_overlay_agent_pins_its_render_to_the_revision_its_launch_recorded(tmp_path):
    agent = store.AgentRecord(
        "engineer@a1b2c3-0001",
        "eng",
        "t1",
        harness="claude",
        profile="engineer",
        conversation_id="conv-1",
        overlays=["tuner"],
        profile_decision={"bundle_revision": "abc123"},
    )
    assert _passed_revision(_resumed(tmp_path, agent)) == ["abc123"]


def test_a_resumed_agent_without_overlays_leaves_its_render_unpinned(tmp_path):
    agent = store.AgentRecord(
        "engineer@a1b2c3-0001",
        "eng",
        "t1",
        harness="claude",
        profile="engineer",
        conversation_id="conv-1",
        profile_decision={"bundle_revision": "abc123"},
    )
    assert _passed_revision(_resumed(tmp_path, agent)) == []


def test_a_resumed_overlay_agent_without_a_recorded_revision_is_refused(tmp_path):
    agent = store.AgentRecord(
        "engineer@a1b2c3-0001",
        "eng",
        "t1",
        harness="claude",
        profile="engineer",
        conversation_id="conv-1",
        overlays=["tuner"],
    )
    with pytest.raises(tick.SpawnError) as refused:
        _resumed(tmp_path, agent)
    assert str(refused.value) == "an overlay launch needs the bundle commit it renders from, and none was recorded"


@pytest.mark.parametrize(
    ("saved", "recorded"),
    [
        ({"bundle_revision": "abc123", "profile_decision": {"bundle_revision": "def456"}}, "abc123"),
        ({"profile_decision": {"bundle_revision": "def456"}}, "def456"),
        ({"profile_decision": {}}, None),
        ({}, None),
    ],
)
def test_the_recorded_revision_prefers_the_launch_field_over_the_profile_decision(saved, recorded):
    assert runtime._recorded_revision(saved) == recorded
