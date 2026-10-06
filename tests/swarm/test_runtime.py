from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.swarm.runtime import HerdrRuntime
from scripts.swarm.store import AgentRecord


def test_spawn_hands_init_agent_the_swarm_lane_and_task(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTIHOOKS_SWARM", raising=False)
    seen = {}

    def run(argv, **kwargs):
        seen["env"] = kwargs.get("env")
        return SimpleNamespace(returncode=0, stdout="status=started\nroute_status=routed\n", stderr="")

    runtime = HerdrRuntime(home=tmp_path, run=run, choose=lambda *_: ("claude", "open"))
    config = SimpleNamespace(
        slug="swarm-buildout", repo=str(tmp_path), code="a1b2c3", compact_limit=0, lanes={}, autonomy="delegate"
    )
    runtime.spawn(config, "eng", "swarm-buildout-eng-4", {"id": "t4", "title": "x"})
    assert seen["env"]["AGENTIHOOKS_SWARM"] == "swarm-buildout"
    assert seen["env"]["AGENTIHOOKS_SWARM_LANE"] == "eng"
    assert seen["env"]["AGENTIHOOKS_SWARM_TASK"] == "t4"


def test_status_and_nudge_in_a_long_named_swarm_reach_the_engineer_not_the_master(tmp_path):
    calls = []
    runtime = HerdrRuntime(home=tmp_path, herdr=lambda args: calls.append(args) or {"agent_status": "idle"})
    slug = "okay-we-re-going-to-mossy-rabin-2026-10-05"
    eng, master = AgentRecord(f"{slug}-eng-4", "eng", "t"), AgentRecord(f"{slug}-master-1", "master", "")
    runtime.status(eng), runtime.nudge(eng, "wake"), runtime.status(master), runtime.nudge(master, "wake")
    assert [c[:2] for c in calls] == [["agent", "get"], ["agent", "prompt"]] * 2
    assert calls[0][2] == calls[1][2] != calls[2][2] == calls[3][2]
    assert calls[0][2].endswith("-eng-4")


def test_status_and_nudge_address_the_agents_own_pane_id(tmp_path):
    herdr = NamedPanes(
        {"w:p9": {"name": "engineer@a1b2c3-0001", "agent_status": "idle"}, "w:p1": {"name": "engineer@a1b2c3-0001"}}
    )
    prompts = []
    runtime = HerdrRuntime(
        home=tmp_path,
        herdr=lambda args: prompts.append(args) or {} if args[:2] == ["agent", "prompt"] else herdr(args),
    )
    eng = AgentRecord("engineer@a1b2c3-0001", "eng", "t", pane_id="w:p9")
    assert runtime.status(eng) == "idle"
    runtime.nudge(eng, "wake")
    assert prompts == [["agent", "prompt", "w:p9", "wake"]]


class NamedPanes:
    def __init__(self, panes):
        self.panes, self.renamed = panes, []

    def __call__(self, args):
        if args[:2] == ["agent", "rename"]:
            self.panes[args[2]]["name"] = args[3]
            self.renamed.append(args[2:])
            return {}
        target = args[2]
        for pane_id, pane in self.panes.items():
            if target in (pane_id, pane.get("name")):
                return {"agent": {**pane, "pane_id": pane_id}}
        raise RuntimeError("agent_not_found")


def test_status_names_a_running_pane_spawned_without_its_herdr_name(tmp_path):
    from scripts.swarm.runtime import herdr_target

    slug = "okay-we-re-going-to-mossy-rabin-2026-10-05"
    herdr = NamedPanes(
        {
            "w:p1": {"name": slug[:32], "agent_status": "idle"},
            "w:p4": {"agent_status": "working", "agent_session": {"kind": "id", "value": "c3"}},
            "w:p5": {"name": "someone-else", "agent_status": "idle"},
            "w:p6": {"name": "okay", "agent_status": "idle"},
            "w:p7": {"agent_status": "idle", "agent_session": {"kind": "id", "value": "other"}},
        }
    )
    runtime = HerdrRuntime(home=tmp_path, herdr=herdr)

    def agent(seat, pane, conversation=""):
        return SimpleNamespace(name=f"{slug}-{seat}", pane_id=pane, conversation_id=conversation)

    assert runtime.status(agent("eng-3", "w:p4", "c3")) == "working"
    assert runtime.status(agent("master-1", "w:p1")) == "idle"
    assert runtime.status(agent("eng-4", "w:p5")) == "unknown"
    assert runtime.status(agent("eng-5", "w:p6")) == "unknown"
    assert runtime.status(agent("eng-6", "w:p7", "c6")) == "unknown"
    assert herdr.renamed == [
        ["w:p4", herdr_target(f"{slug}-eng-3")],
        ["w:p1", herdr_target(f"{slug}-master-1")],
    ]


def test_a_pane_already_carrying_its_herdr_name_is_not_renamed(tmp_path):
    herdr = NamedPanes({"w:p1": {"name": "master-a1b2c3-0001", "agent_status": "idle"}})
    runtime = HerdrRuntime(home=tmp_path, herdr=herdr)
    assert runtime.name_pane(SimpleNamespace(name="master@a1b2c3-0001", pane_id="w:p1", conversation_id="")) is False
    assert herdr.renamed == []


def test_spawn_records_the_model_and_effort_init_agent_launched_with(tmp_path):
    out = "status=started\nroute_status=routed\npane_id=w1:p2\naccount=a\nmodel=opus\neffort=high\n"
    runtime = HerdrRuntime(
        home=tmp_path,
        run=lambda argv, **kw: SimpleNamespace(returncode=0, stdout=out, stderr=""),
        choose=lambda *_: ("claude", "open"),
    )
    config = SimpleNamespace(
        slug="sw", repo=str(tmp_path), code="a1b2c3", compact_limit=0, lanes={}, autonomy="delegate"
    )
    placed = runtime.spawn(config, "eng", "engineer@a1b2c3-0001", {"id": "t1", "title": "x"})
    assert (placed.model, placed.effort) == ("opus", "high")


def _spawn_env(tmp_path, monkeypatch, **config):
    monkeypatch.delenv("AGENTIHOOKS_COMPACT_LIMIT", raising=False)
    seen = {}

    def run(argv, **kwargs):
        seen["env"] = kwargs.get("env")
        return SimpleNamespace(returncode=0, stdout="status=started\nroute_status=routed\n", stderr="")

    runtime = HerdrRuntime(home=tmp_path, run=run, choose=lambda *_: ("claude", "open"))
    runtime.spawn(
        SimpleNamespace(slug="sw", repo=str(tmp_path), code="a1b2c3", lanes={}, **{"autonomy": "delegate", **config}),
        "eng",
        "engineer@a1b2c3-0001",
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
    config = SimpleNamespace(
        slug="sw", repo=str(tmp_path), code="a1b2c3", compact_limit=0, lanes=lanes, autonomy="delegate"
    )
    runtime.spawn(config, lane, "engineer@a1b2c3-0001", {"id": "t1", "title": "x"})
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
    assert seen["requested"] == "" and _passed(seen["argv"]) == ["--model", "opus", "--effort", "high"]


def test_a_lane_without_an_entry_falls_back_to_the_automatic_choice(tmp_path):
    seen = _spawn_seen(tmp_path, {"eng": {"agent": "codex"}}, lane="ci")
    assert seen["requested"] == "" and _passed(seen["argv"]) == ["--model", "opus", "--effort", "high"]


def test_a_lane_model_goes_to_the_automatically_chosen_agent(tmp_path):
    seen = _spawn_seen(tmp_path, {"eng": {"agent": "auto", "model": "sonnet", "effort": "auto"}})
    assert seen["requested"] == "" and _passed(seen["argv"]) == ["--model", "sonnet", "--effort", "high"]


def test_the_lane_role_replaces_the_default_role_in_the_prompt(tmp_path):
    _spawn_seen(tmp_path, {"eng": {"role": "a reviewer who only reads"}})
    text = (tmp_path / "sw" / "prompts" / "engineer@a1b2c3-0001.md").read_text()
    assert text.startswith("You are engineer@a1b2c3-0001, a reviewer who only reads in swarm sw,")


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
        code="a1b2c3",
        compact_limit=0,
        lanes={"eng": {"agent": "auto"}},
        autonomy="delegate",
        codex_share=None,
        codex_min_week_left=None,
    )
    runtime.spawn(config, "eng", "engineer@a1b2c3-0001", {"id": "t1", "title": "x"}, spawns={"claude": 2})
    assert seen["requested"] == "" and seen["spawns"] == {"claude": 2}
    assert (seen["share"], seen["min_week_left"]) == (30, 7)
    assert seen["argv"][seen["argv"].index("--agent") + 1] == "codex"


def _listed(pane_id, session):
    return {"name": pane_id, "pane_id": pane_id, "agent": "claude", "agent_status": "working", "agent_session": session}


def test_conversations_maps_each_pane_herdr_lists_to_its_conversation_id(tmp_path):
    seen = []
    agents = [
        _listed("w1:p1", {"agent": "claude", "kind": "id", "source": "herdr:claude", "value": "5c90d80c"}),
        _listed("w1:p2", {"agent": "codex", "kind": "id", "source": "herdr:codex", "value": "019a-codex"}),
        _listed("w1:p3", {"agent": "claude", "kind": "path", "value": "/some/transcript.jsonl"}),
        _listed("w1:p4", {"agent": "claude", "kind": "id", "value": ""}),
        _listed("w1:p5", None),
        {"pane_id": "w1:p6", "agent": "claude"},
    ]
    runtime = HerdrRuntime(home=tmp_path, herdr=lambda args: seen.append(args) or {"agents": agents})
    assert runtime.conversations() == {
        "w1:p1": "5c90d80c",
        "w1:p2": "019a-codex",
        "w1:p3": "",
        "w1:p4": "",
        "w1:p5": "",
        "w1:p6": "",
    }
    assert seen == [["agent", "list"]]


def test_conversations_is_none_when_herdr_cannot_answer(tmp_path):
    def down(args):
        raise RuntimeError("herdr: no server")

    assert HerdrRuntime(home=tmp_path, herdr=down).conversations() is None


def _resuming(tmp_path, reported, harness="claude"):
    from scripts.swarm.store import AgentRecord

    seen = {"runs": [], "herdr": []}

    def run(argv, **kwargs):
        seen["runs"].append(argv)
        seen.setdefault("env", kwargs.get("env"))
        out = "status=started\nroute_status=routed\npane_id=w2:p9\naccount=a1\nmodel=opus\neffort=high\n"
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    def herdr(args):
        seen["herdr"].append(args)
        return {"agents": [_listed("w2:p9", {"kind": "id", "value": reported})]}

    runtime = HerdrRuntime(home=tmp_path, run=run, herdr=herdr, choose=lambda *_: ("claude", "open"))
    runtime.sleep = lambda seconds: None
    config = SimpleNamespace(
        slug="sw", repo=str(tmp_path), code="a1b2c3", compact_limit=0, lanes={}, autonomy="delegate"
    )
    agent = AgentRecord(
        "engineer@a1b2c3-0001",
        "eng",
        "t1",
        harness=harness,
        account="a1",
        model="opus",
        effort="high",
        conversation_id="c0ffee",
    )
    return runtime, config, agent, seen


def test_resume_relaunches_the_same_harness_name_task_and_account_into_its_conversation(tmp_path):
    runtime, config, agent, seen = _resuming(tmp_path, "c0ffee", harness="codex")
    placed = runtime.resume(config, agent, "you were restored")
    argv = seen["runs"][0]
    assert argv[argv.index("--name") + 1] == "engineer@a1b2c3-0001"
    assert argv[argv.index("--agent") + 1] == "codex"
    assert argv[argv.index("--resume") + 1] == "c0ffee"
    assert argv[argv.index("--dir") + 1] == str(tmp_path)
    assert argv[argv.index("--workspace") + 1] == f"{tmp_path.name}-a1b2c3"
    assert _passed(argv)[:2] == ["--route", "a1"] and "-m" in _passed(argv)
    assert Path(argv[argv.index("--prompt-file") + 1]).read_text() == "you were restored"
    assert (seen["env"]["AGENTIHOOKS_SWARM_LANE"], seen["env"]["AGENTIHOOKS_SWARM_TASK"]) == ("eng", "t1")
    assert (placed.pane_id, placed.harness, placed.account) == ("w2:p9", "codex", "a1")


def test_resume_without_an_account_lets_the_router_pick_one(tmp_path):
    from dataclasses import replace

    runtime, config, agent, seen = _resuming(tmp_path, "c0ffee")
    runtime.resume(config, replace(agent, account=""), "you were restored")
    assert "--route" not in _passed(seen["runs"][0])


def test_a_resume_herdr_never_shows_in_its_conversation_is_closed_and_fails(tmp_path):
    from scripts.swarm.tick import SpawnError

    runtime, config, agent, seen = _resuming(tmp_path, "someone-else")
    with pytest.raises(SpawnError, match="conversation c0ffee"):
        runtime.resume(config, agent, "you were restored")
    assert any("terminate-agent" in argv for argv in seen["runs"])
    assert ["pane", "close", "w2:p9"] in seen["herdr"]


def _launched(tmp_path, monkeypatch, lane, task, lanes=None):
    for key in ("AGENTIHOOKS_CLAUDE_MODEL", "AGENTIHOOKS_CLAUDE_EFFORT"):
        monkeypatch.delenv(key, raising=False)
    seen = {}

    def run(argv, **kwargs):
        seen["argv"] = argv
        return SimpleNamespace(returncode=0, stdout="status=started\nroute_status=routed\n", stderr="")

    runtime = HerdrRuntime(home=tmp_path, run=run, choose=lambda *_: ("claude", "open"))
    auto = {"agent": "auto", "model": "auto", "effort": "auto"}
    config = SimpleNamespace(
        slug="sw",
        repo=str(tmp_path),
        code="a1b2c3",
        compact_limit=0,
        lanes=lanes if lanes is not None else {name: auto for name in ("eng", "ci", "plan", "master")},
        autonomy="delegate",
    )
    runtime.spawn(config, lane, "agent@a1b2c3-0001", task)
    return _passed(seen["argv"])


SEAT_TASKS = {
    "eng": {"id": "t1", "title": "x"},
    "ci": {"id": "t1", "title": "x"},
    "plan": {"id": "t1", "title": "x"},
    "master": {"id": "master", "peer": ""},
}


@pytest.mark.parametrize("lane", sorted(SEAT_TASKS))
@pytest.mark.parametrize("handoff", [None, "# Handoff\n<!-- handoff complete -->"])
def test_every_seat_launch_carries_opus_and_high_effort_for_claude(tmp_path, monkeypatch, lane, handoff):
    task = {**SEAT_TASKS[lane], **({"handoff": handoff} if handoff else {})}
    assert _launched(tmp_path, monkeypatch, lane, task) == ["--model", "opus", "--effort", "high"]


@pytest.mark.parametrize("lane", ["master", "plan"])
def test_master_and_plan_seats_never_take_the_task_classifier_pick(tmp_path, monkeypatch, lane):
    from scripts.swarm import model_pick

    monkeypatch.setattr(model_pick, "pick", lambda *a, **kw: pytest.fail(f"{lane} seat consulted the classifier"))
    task = {**SEAT_TASKS[lane], "handoff": "# Handoff\n<!-- handoff complete -->"}
    assert _launched(tmp_path, monkeypatch, lane, task) == ["--model", "opus", "--effort", "high"]


def test_a_swarm_config_model_wins_over_the_seat_default(tmp_path, monkeypatch):
    lanes = {"master": {"agent": "claude", "model": "sonnet", "effort": "max"}}
    assert _launched(tmp_path, monkeypatch, "master", SEAT_TASKS["master"], lanes) == [
        "--model",
        "sonnet",
        "--effort",
        "max",
    ]
