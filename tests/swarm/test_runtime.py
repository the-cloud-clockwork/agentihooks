from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.swarm.runtime import PLAN_MODE, HerdrRuntime
from scripts.swarm.store import AgentRecord
from tests.swarm.profile_fixture import validated


def test_spawn_hands_init_agent_the_swarm_lane_and_task(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTIHOOKS_SWARM", raising=False)
    seen = {}

    def run(argv, **kwargs):
        seen["env"] = kwargs.get("env")
        return SimpleNamespace(returncode=0, stdout=validated(argv, "status=started\nroute_status=routed\n"), stderr="")

    runtime = HerdrRuntime(home=tmp_path, run=run, choose=lambda *_: ("claude", "open"))
    config = SimpleNamespace(
        slug="swarm-buildout",
        repo=str(tmp_path),
        code="a1b2c3",
        compact_limit=0,
        codex_share=None,
        codex_min_week_left=0,
        lanes={},
        autonomy="delegate",
    )
    runtime.spawn(config, "eng", "swarm-buildout-eng-4", {"id": "t4", "title": "x"})
    assert seen["env"]["AGENTIHOOKS_SWARM"] == "swarm-buildout"
    assert seen["env"]["AGENTIHOOKS_SWARM_LANE"] == "eng"
    assert seen["env"]["AGENTIHOOKS_SWARM_TASK"] == "t4"


@pytest.mark.parametrize(
    ("lanes", "task", "profile"),
    [
        ({}, {"profile": "frontend"}, "frontend"),
        ({"eng": {"profile": "qa"}}, {"profile": "frontend"}, "frontend"),
        ({"eng": {"profile": "qa"}}, {"profile": ""}, "qa"),
        ({}, {}, "engineer"),
    ],
)
def test_a_task_profile_wins_over_the_lane_profile_at_spawn(tmp_path, lanes, task, profile):
    seen = {}

    def run(argv, **kwargs):
        seen["argv"] = argv
        return SimpleNamespace(returncode=0, stdout=validated(argv, "status=started\nroute_status=routed\n"), stderr="")

    runtime = HerdrRuntime(home=tmp_path, run=run, choose=lambda *_: ("claude", "open"))
    config = SimpleNamespace(
        slug="sw",
        repo=str(tmp_path),
        code="a1b2c3",
        compact_limit=0,
        codex_share=None,
        codex_min_week_left=0,
        lanes=lanes,
        autonomy="delegate",
    )
    runtime.spawn(config, "eng", "sw-eng-1", {"id": "t1", "title": "x", **task})
    argv = seen["argv"]
    assert argv[argv.index("--profile") + 1] == profile


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
    assert prompts == [["agent", "prompt", "w:p9", "[swarm delivery] wake"]]


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
        run=lambda argv, **kw: SimpleNamespace(returncode=0, stdout=validated(argv, out), stderr=""),
        choose=lambda *_: ("claude", "open"),
    )
    config = SimpleNamespace(
        slug="sw",
        repo=str(tmp_path),
        code="a1b2c3",
        compact_limit=0,
        codex_share=None,
        codex_min_week_left=0,
        lanes={},
        autonomy="delegate",
    )
    placed = runtime.spawn(config, "eng", "engineer@a1b2c3-0001", {"id": "t1", "title": "x"})
    assert (placed.model, placed.effort) == ("opus", "high")


def test_a_codex_spawn_records_the_model_and_effort_init_agent_launched_with(tmp_path):
    out = "status=started\nroute_status=routed\npane_id=w1:p2\nagent=codex\nmodel=gpt-6.1-sol\neffort=high\n"
    runtime = HerdrRuntime(
        home=tmp_path,
        run=lambda argv, **kw: SimpleNamespace(returncode=0, stdout=validated(argv, out), stderr=""),
        choose=lambda *_: ("codex", "open"),
    )
    config = SimpleNamespace(
        slug="sw",
        repo=str(tmp_path),
        code="a1b2c3",
        compact_limit=0,
        codex_share=None,
        codex_min_week_left=0,
        lanes={},
        autonomy="delegate",
    )
    placed = runtime.spawn(config, "eng", "engineer@a1b2c3-0001", {"id": "t1", "title": "x"})
    assert (placed.harness, placed.model, placed.effort) == ("codex", "gpt-6.1-sol", "high")


def _spawn_env(tmp_path, monkeypatch, **config):
    monkeypatch.delenv("AGENTIHOOKS_COMPACT_LIMIT", raising=False)
    seen = {}

    def run(argv, **kwargs):
        seen["env"] = kwargs.get("env")
        return SimpleNamespace(returncode=0, stdout=validated(argv, "status=started\nroute_status=routed\n"), stderr="")

    runtime = HerdrRuntime(home=tmp_path, run=run, choose=lambda *_: ("claude", "open"))
    runtime.spawn(
        SimpleNamespace(
            slug="sw",
            repo=str(tmp_path),
            code="a1b2c3",
            lanes={},
            **{"autonomy": "delegate", "codex_share": None, "codex_min_week_left": 0, **config},
        ),
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
        return SimpleNamespace(returncode=0, stdout=validated(argv, "status=started\nroute_status=routed\n"), stderr="")

    def choose(requested, environ):
        seen["requested"] = requested
        return requested or "claude", "requested" if requested else "priority"

    runtime = HerdrRuntime(home=tmp_path, run=run, choose=choose)
    config = SimpleNamespace(
        slug="sw",
        repo=str(tmp_path),
        code="a1b2c3",
        compact_limit=0,
        codex_share=None,
        codex_min_week_left=0,
        lanes=lanes,
        autonomy="delegate",
    )
    runtime.spawn(config, lane, "engineer@a1b2c3-0001", {"id": "t1", "title": "x"})
    return seen


def _passed(argv):
    return argv[argv.index("--") + 1 :] if "--" in argv else []


def test_spawn_passes_a_codex_lane_agent_model_and_effort_to_init_agent(tmp_path):
    seen = _spawn_seen(tmp_path, {"eng": {"agent": "codex", "model": "gpt-6.1-sol", "effort": "medium"}})
    assert seen["requested"] == "codex"
    assert seen["argv"][seen["argv"].index("--agent") + 1] == "codex"
    assert _passed(seen["argv"]) == ["-m", "gpt-6.1-sol", "-c", 'model_reasoning_effort="medium"']


def test_spawn_passes_a_claude_lane_model_and_effort_to_init_agent(tmp_path):
    seen = _spawn_seen(tmp_path, {"eng": {"agent": "claude", "model": "sonnet", "effort": "medium"}})
    assert seen["requested"] == "claude"
    assert _passed(seen["argv"]) == ["--model", "sonnet", "--effort", "medium"]


def test_a_claude_planner_starts_in_plan_mode(tmp_path):
    passed = _passed(_spawn_seen(tmp_path, {"plan": {"agent": "claude"}}, lane="plan")["argv"])
    assert passed[passed.index("--permission-mode") + 1] == "plan"


@pytest.mark.parametrize(("lane", "agent"), [("eng", "claude"), ("ci", "claude"), ("plan", "codex")])
def test_only_a_claude_planner_starts_in_plan_mode(tmp_path, lane, agent):
    assert "--permission-mode" not in _passed(_spawn_seen(tmp_path, {lane: {"agent": agent}}, lane=lane)["argv"])


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
        return SimpleNamespace(returncode=0, stdout=validated(argv, "status=started\nroute_status=routed\n"), stderr="")

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


@pytest.mark.parametrize(
    ("reason", "choice"),
    [("fallthrough: claude is at its session cap", "overflow"), ("codex share 0/2 below 30%", "share")],
)
def test_a_spawn_carries_the_router_choice_kind(tmp_path, monkeypatch, reason, choice):
    from scripts import agent_choice

    def run(argv, **kwargs):
        return SimpleNamespace(returncode=0, stdout=validated(argv, "status=started\nroute_status=routed\n"), stderr="")

    monkeypatch.setattr(agent_choice, "choose_shared", lambda *args, **kwargs: ("codex", reason))
    runtime = HerdrRuntime(home=tmp_path, run=run, choose=lambda *_: ("claude", "priority"))
    config = SimpleNamespace(
        slug="sw",
        repo=str(tmp_path),
        code="a1b2c3",
        compact_limit=0,
        lanes={"eng": {"agent": "auto"}},
        autonomy="delegate",
        codex_share=20,
        codex_min_week_left=5,
    )
    placed = runtime.spawn(config, "eng", "engineer@a1b2c3-0001", {"id": "t1", "title": "x"}, spawns={})
    assert placed.choice == choice


@pytest.mark.parametrize(
    ("profile", "lane_agent", "harness"),
    [("frontend", "auto", "claude"), ("frontend", "codex", "claude"), ("engineer", "auto", "codex")],
)
def test_a_task_naming_a_claude_only_profile_spawns_on_claude_at_codex_share_one_hundred(
    tmp_path, monkeypatch, profile, lane_agent, harness
):
    from scripts import agent_choice
    from scripts.profiles import plugins

    monkeypatch.setattr(plugins, "claude_only", lambda name: name == "frontend")
    monkeypatch.setattr(agent_choice, "at_cap", lambda *_: False)
    monkeypatch.setattr(agent_choice, "codex_week_left", lambda *_: 90.0)
    seen = {}

    def run(argv, **kwargs):
        seen["argv"] = argv
        return SimpleNamespace(returncode=0, stdout=validated(argv, "status=started\nroute_status=routed\n"), stderr="")

    runtime = HerdrRuntime(home=tmp_path, run=run, choose=lambda requested, environ: (requested or "claude", "x"))
    config = SimpleNamespace(
        slug="sw",
        repo=str(tmp_path),
        code="a1b2c3",
        compact_limit=0,
        lanes={"eng": {"agent": lane_agent}},
        autonomy="delegate",
        codex_share=100,
        codex_min_week_left=5,
    )
    task = {"id": "t1", "title": "x", "profile": profile}
    placed = runtime.spawn(config, "eng", "engineer@a1b2c3-0001", task, spawns={"claude": 3})
    assert seen["argv"][seen["argv"].index("--agent") + 1] == harness
    assert placed.harness == harness


@pytest.mark.parametrize(("lane", "spawns"), [("eng", {"claude": 3}), ("ci", {}), ("plan", {}), ("master", None)])
@pytest.mark.parametrize("lane_agent", ["auto", "codex"])
def test_a_zero_codex_share_spawns_claude_when_the_picker_chooses_codex(
    tmp_path, monkeypatch, lane, spawns, lane_agent
):
    from scripts import agent_choice

    monkeypatch.setattr(agent_choice, "codex_week_left", lambda *_: 90.0)
    seen = {}

    def run(argv, **kwargs):
        seen["argv"] = argv
        return SimpleNamespace(returncode=0, stdout=validated(argv, "status=started\nroute_status=routed\n"), stderr="")

    runtime = HerdrRuntime(home=tmp_path, run=run, choose=lambda requested, environ: ("codex", "priority"))
    config = SimpleNamespace(
        slug="sw",
        repo=str(tmp_path),
        code="a1b2c3",
        compact_limit=0,
        lanes={lane: {"agent": lane_agent}},
        autonomy="delegate",
        codex_share=0,
        codex_min_week_left=5,
    )
    task = {**SEAT_TASKS[lane], "profile": "engineer"}
    placed = runtime.spawn(config, lane, "engineer@a1b2c3-0001", task, spawns=spawns)
    assert seen["argv"][seen["argv"].index("--agent") + 1] == "claude"
    assert placed.harness == "claude"


@pytest.mark.parametrize(("share", "swarm_share", "capacity"), [(0, "30", False), (30, "0", True), (None, "0", False)])
def test_a_free_codex_slot_counts_only_while_the_codex_share_allows_codex(
    tmp_path, monkeypatch, share, swarm_share, capacity
):
    from scripts import agent_choice

    monkeypatch.setattr(agent_choice, "codex_week_left", lambda *_: 90.0)
    monkeypatch.setenv("AGENTIHOOKS_SWARM_CODEX_SHARE", swarm_share)

    def choose(requested, environ):
        if requested:
            return requested, "requested"
        if environ.get("AGENTIHOOKS_AGENT_PRIORITY") == "claude":
            return "claude", agent_choice.ALL_FULL
        return "codex", "fallthrough: claude is at its session cap"

    runtime = HerdrRuntime(home=tmp_path, choose=choose)
    config = SimpleNamespace(codex_share=share, codex_min_week_left=5)
    assert runtime.has_capacity(config) is capacity


def test_a_claude_only_profile_asks_the_plain_choice_for_claude_with_the_environment(tmp_path, monkeypatch):
    from scripts.profiles import plugins

    monkeypatch.setattr(plugins, "claude_only", lambda name: name == "frontend")
    monkeypatch.setenv("AGENTIHOOKS_PROBE", "1")
    seen = {}

    def choose(requested, environ):
        seen.update(requested=requested, probe=environ.get("AGENTIHOOKS_PROBE"))
        return requested, "requested"

    def run(argv, **kwargs):
        return SimpleNamespace(returncode=0, stdout=validated(argv, "status=started\nroute_status=routed\n"), stderr="")

    runtime = HerdrRuntime(home=tmp_path, run=run, choose=choose)
    config = SimpleNamespace(
        slug="sw",
        repo=str(tmp_path),
        code="a1b2c3",
        compact_limit=0,
        codex_share=None,
        codex_min_week_left=0,
        lanes={"eng": {"agent": "codex"}},
        autonomy="delegate",
    )
    runtime.spawn(config, "eng", "engineer@a1b2c3-0001", {"id": "t1", "title": "x", "profile": "frontend"})
    assert seen == {"requested": "claude", "probe": "1"}


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
        return SimpleNamespace(returncode=0, stdout=validated(argv, out), stderr="")

    def herdr(args):
        seen["herdr"].append(args)
        return {"agents": [_listed("w2:p9", {"kind": "id", "value": reported})]}

    runtime = HerdrRuntime(home=tmp_path, run=run, herdr=herdr, choose=lambda *_: ("claude", "open"))
    runtime.sleep = lambda seconds: None
    config = SimpleNamespace(
        slug="sw",
        repo=str(tmp_path),
        code="a1b2c3",
        compact_limit=0,
        codex_share=None,
        codex_min_week_left=0,
        lanes={},
        autonomy="delegate",
    )
    agent = AgentRecord(
        "engineer@a1b2c3-0001",
        "eng",
        "t1",
        harness=harness,
        account="a1",
        model="opus",
        effort="high",
        profile="engineer",
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


@pytest.mark.parametrize(("recorded", "profile"), [("frontend", "frontend"), ("engineer", "engineer")])
def test_resume_relaunches_on_the_profile_the_agent_was_spawned_with(tmp_path, recorded, profile):
    from dataclasses import replace

    runtime, config, agent, seen = _resuming(tmp_path, "c0ffee")
    runtime.resume(config, replace(agent, profile=recorded), "you were restored")
    argv = seen["runs"][0]
    assert argv[argv.index("--profile") + 1] == profile


def test_a_proof_swarm_agent_launches_into_the_space_named_by_its_slug(tmp_path):
    runtime, config, agent, seen = _resuming(tmp_path, "c0ffee", harness="codex")
    config.slug = "proof-a1b2c3-dn1-1"
    runtime.resume(config, agent, "you were restored")
    argv = seen["runs"][0]
    assert argv[argv.index("--workspace") + 1] == "proof-a1b2c3-dn1-1"


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


@pytest.mark.parametrize(
    "lane,harness,launch,source",
    [
        ("master", "claude", ["--model", "opus", "--effort", "high"], "frontier"),
        ("master", "codex", ["-m", "gpt-6.1-sol", "-c", 'model_reasoning_effort="high"'], "frontier"),
        ("eng", "claude", ["--model", "opus", "--effort", "high"], "lane-default"),
        ("eng", "codex", ["-m", "gpt-6.1-sol", "-c", 'model_reasoning_effort="high"'], "lane-default"),
    ],
)
def test_resume_uses_defaults_when_no_resolved_model_was_recorded(tmp_path, monkeypatch, lane, harness, launch, source):
    from dataclasses import replace

    for key in ("CLAUDE_MODEL", "CLAUDE_EFFORT", "CODEX_MODEL", "CODEX_EFFORT"):
        monkeypatch.delenv(f"AGENTIHOOKS_{key}", raising=False)
    runtime, config, agent, seen = _resuming(tmp_path, "c0ffee", harness=harness)
    config.lanes = {"master": {"model": "sonnet", "effort": "low"}}
    placed = runtime.resume(config, replace(agent, lane=lane, model="", effort=""), "you were restored")
    assert _passed(seen["runs"][0]) == ["--route", "a1", *launch]
    assert placed.model_source == source


def test_resume_relaunches_on_the_model_and_effort_its_lane_names(tmp_path):
    from dataclasses import replace

    runtime, config, agent, seen = _resuming(tmp_path, "c0ffee")
    config.lanes = {"eng": {"model": "fable", "effort": "medium"}}
    runtime.resume(config, replace(agent, model="", effort=""), "you were restored")
    assert _passed(seen["runs"][0]) == ["--route", "a1", "--model", "fable", "--effort", "medium"]


def test_resume_preserves_the_recorded_resolved_model_and_effort(tmp_path):
    from dataclasses import replace

    runtime, config, agent, seen = _resuming(tmp_path, "c0ffee")
    config.lanes = {"eng": {"model": "opus", "effort": "high"}}
    runtime.resume(config, replace(agent, model="sonnet", effort="medium"), "continue")
    assert _passed(seen["runs"][0]) == ["--route", "a1", "--model", "sonnet", "--effort", "medium"]


def _launched(tmp_path, monkeypatch, lane, task, lanes=None, harness="claude", env=None):
    for key in ("MODEL", "EFFORT"):
        for agent in ("CLAUDE", "CODEX"):
            monkeypatch.delenv(f"AGENTIHOOKS_{agent}_{key}", raising=False)
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    seen = {}

    def run(argv, **kwargs):
        seen["argv"] = argv
        return SimpleNamespace(returncode=0, stdout=validated(argv, "status=started\nroute_status=routed\n"), stderr="")

    runtime = HerdrRuntime(home=tmp_path, run=run, choose=lambda *_: (harness, "open"))
    auto = {"agent": "auto", "model": "auto", "effort": "auto"}
    config = SimpleNamespace(
        slug="sw",
        repo=str(tmp_path),
        code="a1b2c3",
        compact_limit=0,
        codex_share=None,
        codex_min_week_left=0,
        lanes=lanes if lanes is not None else {name: auto for name in ("eng", "ci", "plan", "master")},
        autonomy="delegate",
    )
    if task.get("handoff") and not task.get("handoff_envelope"):
        from scripts.swarm.templates import DEFAULT_PROFILES

        task = {
            **task,
            "handoff_envelope": {
                "launch": {
                    "profile": DEFAULT_PROFILES[lane],
                    "harness": harness,
                    "model": "opus" if harness == "claude" else "gpt-6.1-sol",
                    "effort": "high",
                }
            },
        }
    runtime.spawn(config, lane, "agent@a1b2c3-0001", task)
    passed = _passed(seen["argv"])
    return passed[:-2] if passed[-2:] == PLAN_MODE else passed


SEAT_TASKS = {
    "eng": {"id": "t1", "title": "x"},
    "ci": {"id": "t1", "title": "x"},
    "plan": {"id": "t1", "title": "x"},
    "master": {"id": "master", "peer": ""},
}


@pytest.mark.parametrize("lane", sorted(SEAT_TASKS))
@pytest.mark.parametrize("handoff", [None, "# Handoff\n<!-- handoff complete -->"])
def test_every_seat_launch_carries_opus_and_high_effort_for_claude(tmp_path, monkeypatch, lane, handoff):
    from scripts.swarm.templates import DEFAULT_PROFILES

    transfer = (
        {
            "handoff": handoff,
            "handoff_envelope": {
                "launch": {
                    "profile": DEFAULT_PROFILES[lane],
                    "harness": "claude",
                    "model": "opus",
                    "effort": "high",
                }
            },
        }
        if handoff
        else {}
    )
    task = {**SEAT_TASKS[lane], **transfer}
    assert _launched(tmp_path, monkeypatch, lane, task) == ["--model", "opus", "--effort", "high"]


@pytest.mark.parametrize("lane", ["master", "plan"])
def test_master_and_plan_seats_never_take_the_task_classifier_pick(tmp_path, monkeypatch, lane):
    from scripts.swarm import model_pick

    monkeypatch.setattr(model_pick, "pick", lambda *a, **kw: pytest.fail(f"{lane} seat consulted the classifier"))
    task = {**SEAT_TASKS[lane], "handoff": "# Handoff\n<!-- handoff complete -->"}
    assert _launched(tmp_path, monkeypatch, lane, task) == ["--model", "opus", "--effort", "high"]


@pytest.mark.parametrize("handoff", [None, "# Handoff\n<!-- handoff complete -->"])
@pytest.mark.parametrize(
    "harness,launch",
    [
        ("claude", ["--model", "opus", "--effort", "high"]),
        ("codex", ["-m", "gpt-6.1-sol", "-c", 'model_reasoning_effort="high"']),
    ],
)
def test_a_master_launches_on_the_frontier_model_at_high_effort_whatever_the_lane_or_environment_names(
    tmp_path, monkeypatch, harness, launch, handoff
):
    lanes = {"master": {"agent": harness, "model": "sonnet", "effort": "low"}}
    env = {f"AGENTIHOOKS_{harness.upper()}_MODEL": "haiku", f"AGENTIHOOKS_{harness.upper()}_EFFORT": "low"}
    task = {**SEAT_TASKS["master"], **({"handoff": handoff} if handoff else {})}
    assert _launched(tmp_path, monkeypatch, "master", task, lanes, harness=harness, env=env) == launch


def _lowest_pick(*args, **kwargs):
    from hooks.classifier import Answer, DecisionResult

    return DecisionResult({"effort": Answer("score", score=0, confidence=0.95)}, "luna")


@pytest.mark.parametrize("lane", sorted(SEAT_TASKS))
@pytest.mark.parametrize(
    "harness,launch",
    [
        ("claude", ["--model", "opus", "--effort", "high"]),
        ("codex", ["-m", "gpt-6.1-sol", "-c", 'model_reasoning_effort="high"']),
    ],
)
def test_a_live_classifier_never_launches_a_seat_below_the_default_model_and_high_effort(
    tmp_path, monkeypatch, lane, harness, launch
):
    from scripts.swarm import model_pick

    monkeypatch.setattr(model_pick, "decide", _lowest_pick)
    assert _launched(tmp_path, monkeypatch, lane, SEAT_TASKS[lane], harness=harness) == launch


def test_a_swarm_config_model_launches_as_named_with_the_classifier_live(tmp_path, monkeypatch):
    from scripts.swarm import model_pick

    monkeypatch.setattr(model_pick, "decide", _lowest_pick)
    lanes = {"eng": {"agent": "claude", "model": "sonnet", "effort": "auto"}}
    assert _launched(tmp_path, monkeypatch, "eng", SEAT_TASKS["eng"], lanes) == [
        "--model",
        "sonnet",
        "--effort",
        "high",
    ]


@pytest.mark.parametrize("lane", sorted(SEAT_TASKS))
@pytest.mark.parametrize("harness", ["claude", "codex"])
@pytest.mark.parametrize("launch", ["spawn", "successor", "resume"])
def test_every_swarm_launch_runs_its_profile_with_brain_on(tmp_path, monkeypatch, lane, harness, launch):
    from scripts import select_profile
    from scripts.swarm import model_pick
    from scripts.swarm.templates import DEFAULT_PROFILES

    seen = {}

    def run(argv, **kwargs):
        seen["argv"] = argv
        return SimpleNamespace(returncode=0, stdout=validated(argv, "status=started\nroute_status=routed\n"), stderr="")

    monkeypatch.setattr(model_pick, "pick", lambda *a, **kw: model_pick.ModelPick("m", "high"))
    runtime = HerdrRuntime(home=tmp_path, run=run, choose=lambda *_: (harness, "open"))
    runtime._holds = lambda *a: True
    config = SimpleNamespace(
        slug="sw",
        repo=str(tmp_path),
        code="a1b2c3",
        compact_limit=0,
        codex_share=None,
        codex_min_week_left=0,
        lanes={},
        autonomy="full",
    )
    if launch == "resume":
        agent = AgentRecord(
            "agent@a1b2c3-0001", lane, "t1", harness=harness, profile=DEFAULT_PROFILES[lane], conversation_id="c0ffee"
        )
        runtime.resume(config, agent, "you were restored")
    else:
        handoff = (
            {
                "handoff": "# Handoff\n<!-- handoff complete -->",
                "handoff_envelope": {
                    "launch": {
                        "profile": DEFAULT_PROFILES[lane],
                        "harness": harness,
                        "model": "opus" if harness == "claude" else "gpt-6.1-sol",
                        "effort": "high",
                    }
                },
            }
            if launch == "successor"
            else {}
        )
        runtime.spawn(config, lane, "agent@a1b2c3-0001", {**SEAT_TASKS[lane], **handoff})
    argv = seen["argv"]
    profile = argv[argv.index("--profile") + 1]
    assert profile == DEFAULT_PROFILES[lane]
    root = tmp_path / profile
    root.mkdir()
    monkeypatch.setattr(select_profile.profiles, "_chain", lambda name: [(name, root)])
    monkeypatch.setattr(select_profile.profiles, "render", lambda *a: None)
    env, _ = select_profile.prepare(profile, argv[argv.index("--agent") + 1], "", "", [], {})
    assert "brain" in env["AGENTIHOOKS_BASE_CHANNELS"].split(",")


@pytest.mark.parametrize(("agent", "named"), [("claude", True), ("codex", False)])
def test_a_claude_spawn_asks_init_agent_for_the_inbox_channel(tmp_path, agent, named):
    argv = _spawn_seen(tmp_path, {"eng": {"agent": agent}})["argv"]
    assert ("--inbox-channel" in argv[: argv.index("--")]) is named


@pytest.mark.parametrize(("harness", "named"), [("claude", True), ("codex", False)])
def test_a_claude_resume_asks_init_agent_for_the_inbox_channel(tmp_path, harness, named):
    runtime, config, agent, seen = _resuming(tmp_path, "c0ffee", harness=harness)
    runtime.resume(config, agent, "you were restored")
    argv = seen["runs"][0]
    assert ("--inbox-channel" in argv[: argv.index("--")]) is named
