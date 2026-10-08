from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.swarm.reaper import Outcome
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
        lanes={},
        autonomy="delegate",
    )
    runtime.spawn(config, "eng", "swarm-buildout-eng-4", {"id": "t4", "title": "x"})
    assert seen["env"]["AGENTIHOOKS_SWARM"] == "swarm-buildout"
    assert seen["env"]["AGENTIHOOKS_SWARM_LANE"] == "eng"
    assert seen["env"]["AGENTIHOOKS_SWARM_TASK"] == "t4"


def test_spawn_stamps_the_launch_start_before_init_agent_runs(tmp_path, monkeypatch):
    clock = iter([5_000.0, 9_000.0])
    monkeypatch.setattr("scripts.swarm.runtime.time.time", lambda: next(clock))

    def run(argv, **kwargs):
        return SimpleNamespace(returncode=0, stdout=validated(argv, "status=started\nroute_status=routed\n"), stderr="")

    runtime = HerdrRuntime(home=tmp_path, run=run, choose=lambda *_: ("codex", "open"))
    config = SimpleNamespace(
        slug="sw",
        repo=str(tmp_path),
        code="a1b2c3",
        compact_limit=0,
        lanes={},
        autonomy="delegate",
    )
    placed = runtime.spawn(config, "eng", "sw-eng-1", {"id": "t1", "title": "x"})
    assert placed.launched_at == 5_000_000


def test_spawn_records_each_launch_step_and_the_host_load(tmp_path, monkeypatch):
    clock = iter([5_000.0, 9_000.0])
    loads = iter([(7.25, 6.5, 5.0), (12.0, 8.0, 6.0)])
    monkeypatch.setattr("scripts.swarm.runtime.time.time", lambda: next(clock))
    monkeypatch.setattr("scripts.swarm.runtime.os.getloadavg", lambda: next(loads))
    out = "status=started\nlauncher_at=5001000\nroute_status=routed\nharness_at=5004000\n"

    def run(argv, **kwargs):
        return SimpleNamespace(returncode=0, stdout=validated(argv, out), stderr="")

    runtime = HerdrRuntime(home=tmp_path, run=run, choose=lambda *_: ("codex", "open"))
    config = SimpleNamespace(
        slug="sw",
        repo=str(tmp_path),
        code="a1b2c3",
        compact_limit=0,
        lanes={},
        autonomy="delegate",
    )
    placed = runtime.spawn(config, "eng", "sw-eng-1", {"id": "t1", "title": "x"})
    assert placed.launch_timings == {
        "launched_at": 5_000_000,
        "launcher_at": 5_001_000,
        "harness_at": 5_004_000,
        "returned_at": 9_000_000,
        "load_at_launch": [7.25, 6.5, 5.0],
        "load_at_return": [12.0, 8.0, 6.0],
    }


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
        lanes={},
        autonomy="delegate",
    )
    placed = runtime.spawn(config, "eng", "engineer@a1b2c3-0001", {"id": "t1", "title": "x"})
    assert (placed.harness, placed.model, placed.effort) == ("codex", "gpt-6.1-sol", "high")


def _spawn_env(tmp_path, monkeypatch, task=None, **config):
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
            **{"autonomy": "delegate", **config},
        ),
        "eng",
        "engineer@a1b2c3-0001",
        task or {"id": "t1", "title": "x"},
    )
    return seen["env"]


def test_spawn_hands_init_agent_the_swarm_compact_limit(tmp_path, monkeypatch):
    assert _spawn_env(tmp_path, monkeypatch, compact_limit=40)["AGENTIHOOKS_COMPACT_LIMIT"] == "40"


def test_spawn_without_a_swarm_compact_limit_leaves_the_default(tmp_path, monkeypatch):
    assert "AGENTIHOOKS_COMPACT_LIMIT" not in _spawn_env(tmp_path, monkeypatch, compact_limit=0)


def test_spawn_marks_the_launch_as_the_tick_s_own(tmp_path, monkeypatch):
    assert _spawn_env(tmp_path, monkeypatch, compact_limit=0)["AGENTIHOOKS_SWARM_SPAWN"] == "1"


def test_spawn_names_the_predecessor_conversation_of_an_attached_transfer(tmp_path, monkeypatch):
    task = {"id": "t1", "title": "x", "transfer": {"id": "x1"}, "handoff_envelope": {"conversation_id": "sess-old"}}
    env = _spawn_env(tmp_path, monkeypatch, task=task, compact_limit=0)
    assert env["AGENTIHOOKS_PREDECESSOR_SESSION"] == "sess-old"


@pytest.mark.parametrize(
    "extra",
    [
        {"handoff_envelope": {"conversation_id": "sess-old"}},
        {"transfer": {"id": "x1"}, "handoff_envelope": {"conversation_id": "unknown"}},
        {"transfer": {"id": "x1"}},
    ],
)
def test_spawn_without_a_known_predecessor_names_none(tmp_path, monkeypatch, extra):
    monkeypatch.setenv("AGENTIHOOKS_PREDECESSOR_SESSION", "sess-inherited")
    env = _spawn_env(tmp_path, monkeypatch, task={"id": "t1", "title": "x", **extra}, compact_limit=0)
    assert "AGENTIHOOKS_PREDECESSOR_SESSION" not in env


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
        lanes=lanes,
        autonomy="delegate",
    )
    runtime.spawn(config, lane, "engineer@a1b2c3-0001", {"id": "t1", "title": "x"})
    return seen


@pytest.mark.parametrize("pin", ["claude", "codex"])
@pytest.mark.parametrize("saved_kind", ["launch_assignment", "handoff_envelope", "none"])
def test_a_lane_pin_wins_over_an_opposite_saved_harness(tmp_path, monkeypatch, pin, saved_kind):
    from scripts.swarm import model_pick

    opposite = "codex" if pin == "claude" else "claude"
    saved = {
        "profile": "engineer",
        "harness": opposite,
        "model": "gpt-6.1-sol" if opposite == "codex" else "opus",
        "effort": "high",
        "account": "old-account",
        "model_source": "old-classifier",
    }
    task = {"id": "t1", "title": "Fix routing", "profile": "engineer"}
    if saved_kind == "launch_assignment":
        task[saved_kind] = saved
    elif saved_kind == "handoff_envelope":
        task[saved_kind] = {"launch": saved}
        task["handoff"] = "Original task context"
    seen = {}

    def run(argv, **kwargs):
        seen["argv"] = argv
        return SimpleNamespace(returncode=0, stdout=validated(argv, "status=started\nroute_status=routed\n"), stderr="")

    def pick(harness, lane, task, environ):
        seen["classifier_harness"] = harness
        return model_pick.ModelPick("", "high", "classifier", 0.9)

    monkeypatch.setattr(model_pick, "pick", pick)
    runtime = HerdrRuntime(
        home=tmp_path,
        run=run,
        choose=lambda requested, environ: (requested or opposite, "requested" if requested else "priority"),
    )
    config = SimpleNamespace(
        slug="sw",
        repo=str(tmp_path),
        code="a1b2c3",
        compact_limit=0,
        lanes={"eng": {"agent": pin, "effort": "auto"}},
        autonomy="delegate",
    )
    placed = runtime.spawn(config, "eng", "engineer@a1b2c3-0001", task)
    argv = seen["argv"]
    assert argv[argv.index("--agent") + 1] == pin
    assert placed.harness == pin
    assert seen["classifier_harness"] == pin
    assert placed.model_source == "classifier"
    assert "--route" not in argv


@pytest.mark.parametrize(("lane", "label"), [("eng", "lane harness"), ("master", "master affinity")])
def test_a_codex_lane_pin_refuses_a_claude_only_profile(tmp_path, monkeypatch, lane, label):
    from scripts.swarm.runtime import plugins
    from scripts.swarm.tick import SpawnError

    monkeypatch.setattr(plugins, "claude_only", lambda profile: True)
    with pytest.raises(SpawnError) as error:
        _spawn_seen(tmp_path, {lane: {"agent": "codex", "profile": "engineer"}}, lane=lane)
    assert str(error.value) == f"{label} codex cannot mount the claude only profile engineer"
    assert error.value.status == "unsupported"


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


@pytest.mark.parametrize(
    ("reason", "choice"),
    [("priority", "other"), ("rotation", "rotation"), ("fallthrough: claude is at its session cap", "overflow")],
)
def test_a_spawn_carries_the_router_choice_kind(tmp_path, reason, choice):
    def run(argv, **kwargs):
        return SimpleNamespace(returncode=0, stdout=validated(argv, "status=started\nroute_status=routed\n"), stderr="")

    runtime = HerdrRuntime(home=tmp_path, run=run, choose=lambda *_: ("codex", reason))
    config = SimpleNamespace(
        slug="sw",
        repo=str(tmp_path),
        code="a1b2c3",
        compact_limit=0,
        lanes={"eng": {"agent": "auto"}},
        autonomy="delegate",
    )
    placed = runtime.spawn(config, "eng", "engineer@a1b2c3-0001", {"id": "t1", "title": "x"})
    assert placed.choice == choice


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
        lanes={"eng": {"agent": "auto"}},
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


def _session(session_id, name):
    from scripts.terminate_agent import Session

    return Session(session_id, "claude", name, None, "", "alive")


def test_a_pane_herdr_gives_no_id_takes_its_named_sessions_main_id(tmp_path, monkeypatch):
    import scripts.terminate_agent

    main = "891dd446-d34e-4a9b-a14f-6868322802a4"
    monkeypatch.setattr(
        scripts.terminate_agent,
        "sessions",
        lambda: [
            _session("ac713ad0096d0dc4b", "master@a1b2c3-0002"),
            _session(main, "master@a1b2c3-0002"),
            _session("0b9b1c64-4c55-4d8d-9a43-0f3f0c1e2a11", ""),
            _session("af0e1c2d-1111-4222-8333-444455556666", "engineer@a1b2c3-0001"),
        ],
    )
    agents = [
        {"name": "master-a1b2c3-0002", "pane_id": "w1:m1", "agent_session": None},
        {
            "name": "engineer-a1b2c3-0001",
            "pane_id": "w1:p1",
            "agent_session": {"kind": "id", "value": "5c90d80c"},
        },
        {"name": "engineer-a1b2c3-0009", "pane_id": "w1:p2", "agent_session": None},
    ]
    runtime = HerdrRuntime(home=tmp_path, herdr=lambda args: {"agents": agents})
    assert runtime.conversations() == {"w1:m1": main, "w1:p1": "5c90d80c", "w1:p2": ""}


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


def test_a_resume_herdr_never_shows_in_its_conversation_is_closed_and_fails(tmp_path, scratch):
    from scripts.swarm.tick import SpawnError

    homes = scratch("t1")
    runtime, config, agent, seen = _resuming(tmp_path, "someone-else")
    ended = []
    runtime.end = lambda name, pid, homes, start=0: ended.append((name, pid, homes)) or Outcome()
    with pytest.raises(SpawnError, match="conversation c0ffee") as error:
        runtime.resume(config, agent, "you were restored")
    assert error.value.status == "ambiguous"
    assert ended == [("engineer@a1b2c3-0001", 123, homes)]
    assert not any("terminate-agent" in argv for argv in seen["runs"])
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


def test_spawn_records_launch_preparation_and_waited_subprocess_cost(tmp_path, monkeypatch, capsys):
    import json

    from scripts.swarm import timing

    with timing.tick("sw"):
        _launched(tmp_path, monkeypatch, "eng", {"id": "t1", "title": "x"})
    rows = [json.loads(line) for line in capsys.readouterr().err.splitlines() if '"swarm_tick_step"' in line]
    finished = {row["step"] for row in rows if row["phase"] == "finished"}
    assert {
        "scripts.swarm.profile_choice.choose",
        "scripts.swarm.prompt.build",
        "scripts.swarm.priming_trace.write",
        "scripts.swarm.model_pick.pick",
        "scripts.swarm.runtime.HerdrRuntime._launch",
    } <= finished
    assert all(row["slug"] == "sw" for row in rows)
    assert all(row["outcome"] == "success" for row in rows if row["phase"] == "finished")


def test_spawn_preserves_profile_and_prompt_boundary_arguments(tmp_path, monkeypatch):
    from scripts.swarm import profile_choice, prompt

    observed = []

    def choose(slug, lane, chosen, task, environ, overlays):
        assert slug == "sw"
        assert lane == "eng"
        assert overlays == {}
        assert task == {"id": "t1", "title": "x"}
        observed.append("profile")
        return profile_choice.ProfileDecision("engineer", "task", "engineering work")

    def build(slug, repo, lane, name, task, *, role, autonomy):
        assert slug == "sw"
        assert repo == str(tmp_path)
        assert lane == "eng"
        assert name == "agent@a1b2c3-0001"
        assert task == {"id": "t1", "title": "x", "harness": "claude"}
        assert role == ""
        assert autonomy == "delegate"
        observed.append("prompt")
        return "fixture prompt"

    monkeypatch.setattr(profile_choice, "choose", choose)
    monkeypatch.setattr(prompt, "build", build)
    _launched(tmp_path, monkeypatch, "eng", {"id": "t1", "title": "x"})
    assert observed == ["profile", "prompt"]


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
def test_every_swarm_launch_subscribes_the_brain_overlay_its_profile_renders(
    tmp_path, monkeypatch, lane, harness, launch
):
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
    root, brain = tmp_path / profile, tmp_path / "brain"
    root.mkdir()
    brain.mkdir()
    (root / "profile.yml").write_text("allowedOverlays: [brain]\n")
    monkeypatch.setattr(select_profile.profiles, "_chain", lambda name: [(name, root), ("brain", brain)])
    monkeypatch.setattr(select_profile.profiles, "render", lambda *a, **kw: None)
    monkeypatch.setattr(select_profile.profiles, "profile_dir", lambda name, worn=(): tmp_path / "rendered" / name)
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


def test_has_capacity_asks_the_rotation_with_this_environment(tmp_path):
    import os

    from scripts import agent_choice

    seen, reasons = [], iter(["rotation", agent_choice.ALL_FULL])
    runtime = HerdrRuntime(
        home=tmp_path, choose=lambda requested, environ: seen.append((requested, environ)) or ("claude", next(reasons))
    )
    assert runtime.has_capacity(None) is True
    assert runtime.has_capacity(None) is False
    assert seen[0] == ("", dict(os.environ))


def test_quota_capacity_reads_this_environment_and_hands_demand_on(tmp_path, monkeypatch):
    import os

    from scripts.swarm import capacity

    seen = {}
    monkeypatch.setattr(
        capacity, "accounts", lambda environ, now, refresh: seen.update(environ=environ, now=now, refresh=refresh) or []
    )
    monkeypatch.setattr(
        capacity,
        "calculate",
        lambda config, rows, agents, demand, requirements, accounts: (
            seen.update(demand=demand) or {"allocation": {}, "placements": {}}
        ),
    )
    runtime = HerdrRuntime(home=tmp_path)
    runtime.quota_capacity(None, [], 5.0, {"eng": 1})
    assert seen == {"environ": dict(os.environ), "now": 5.0, "refresh": True, "demand": {"eng": 1}}
    runtime.quota_capacity(None, [], 5.0, {"eng": 0})
    assert seen["refresh"] is False


def test_rotation_picks_the_fewest_session_seat_or_asks_choose(tmp_path):
    from scripts import agent_choice
    from scripts.swarm import capacity

    calls = []
    runtime = HerdrRuntime(
        home=tmp_path, choose=lambda requested, environ: calls.append(requested) or ("codex", "requested")
    )
    assert runtime._rotation("", {}) == ("codex", "requested")
    runtime._quota_accounts = [
        capacity.Account("claude", "a", "OPEN", 2, 90, 90, 6),
        capacity.Account("codex", "cx", "OPEN", 0, 90, 90, 6),
    ]
    assert runtime._rotation("", {}) == ("codex", "rotation")
    assert runtime._rotation("claude", {}) == ("codex", "requested")
    runtime._quota_accounts = [capacity.Account("codex", "cx", "CLOSED", 0, 90, 90, 0)]
    assert runtime._rotation("", {}) == ("claude", agent_choice.ALL_FULL)
    assert calls == ["", "claude"]


def test_a_saved_account_is_kept_while_it_has_a_seat(tmp_path):
    from scripts.swarm import capacity

    seen = {}

    def run(argv, **kwargs):
        seen["argv"] = argv
        return SimpleNamespace(returncode=0, stdout=validated(argv, "status=started\nroute_status=routed\n"), stderr="")

    saved = {
        "profile": "engineer",
        "harness": "claude",
        "model": "opus",
        "effort": "high",
        "account": "old",
        "model_source": "handoff",
    }
    runtime = HerdrRuntime(
        home=tmp_path, run=run, choose=lambda requested, environ: (requested or "claude", "requested")
    )
    runtime._quota_accounts = [
        capacity.Account("claude", "fresh", "OPEN", 0, 90, 90, 6),
        capacity.Account("claude", "old", "OPEN", 3, 90, 90, 6),
    ]
    config = SimpleNamespace(
        slug="sw", repo=str(tmp_path), code="a1b2c3", compact_limit=0, lanes={}, autonomy="delegate"
    )
    task = {"id": "t1", "title": "x", "profile": "engineer", "launch_assignment": saved}
    runtime.spawn(config, "eng", "engineer@a1b2c3-0001", task)
    argv = seen["argv"]
    assert argv[argv.index("--route") + 1] == "old"


@pytest.mark.parametrize("harness,model", [("claude", "opus"), ("codex", "gpt-6.1-sol")])
@pytest.mark.parametrize("reason,expected", [("quota", "fresh"), ("recycle", "old")])
def test_handoff_account_selection_preserves_run_options(tmp_path, harness, model, reason, expected):
    from scripts.swarm import capacity

    seen = {}

    def run(argv, **kwargs):
        seen["argv"] = argv
        return SimpleNamespace(returncode=0, stdout=validated(argv, "status=started\nroute_status=routed\n"), stderr="")

    saved = {
        "profile": "engineer",
        "harness": harness,
        "model": model,
        "effort": "high",
        "account": "old",
        "overlays": ["brain"],
        "profile_decision": {"bundle_revision": "a" * 40},
    }
    runtime = HerdrRuntime(home=tmp_path, run=run, choose=lambda requested, environ: (requested, "requested"))
    runtime._quota_accounts = [
        capacity.Account(harness, "old", "OPEN", 0, 5, 90, 2),
        capacity.Account(harness, "fresh", "OPEN", 1, 90, 90, 6),
    ]
    config = SimpleNamespace(
        slug="sw", repo=str(tmp_path), code="a1b2c3", compact_limit=0, lanes={}, autonomy="delegate"
    )
    task = {
        "id": "t1",
        "title": "x",
        "handoff": "saved handoff",
        "handoff_envelope": {"reason": reason, "seat": "eng-2@sw", "launch": saved},
        "launch_assignment": saved,
    }
    placed = runtime.spawn(config, "eng", "engineer@a1b2c3-0001", task)
    argv = seen["argv"]
    assert argv[argv.index("--route") + 1] == expected
    assert argv[argv.index("--profile") + 1] == "engineer"
    assert argv[argv.index("--overlay") + 1] == "brain"
    assert argv[argv.index("--agent") + 1] == harness
    if harness == "claude":
        assert argv[argv.index("--model") + 1] == model
        assert argv[argv.index("--effort") + 1] == "high"
    else:
        assert argv[argv.index("-m") + 1] == model
        assert 'model_reasoning_effort="high"' in argv
    assert placed.overlays == ["brain"]
    assert task["handoff_envelope"]["seat"] == "eng-2@sw"


@pytest.mark.parametrize("harness,model", [("claude", "opus"), ("codex", "gpt-6.1-sol")])
def test_quota_handoff_without_another_account_keeps_the_handoff(tmp_path, harness, model):
    from scripts.swarm import capacity
    from scripts.swarm.tick import SpawnError

    saved = {"profile": "engineer", "harness": harness, "model": model, "effort": "high", "account": "old"}
    runtime = HerdrRuntime(
        home=tmp_path,
        run=lambda *args, **kwargs: pytest.fail("must not launch on the depleted account"),
        choose=lambda requested, environ: (requested, "requested"),
    )
    runtime._quota_accounts = [capacity.Account(harness, "old", "OPEN", 0, 5, 90, 2)]
    config = SimpleNamespace(
        slug="sw", repo=str(tmp_path), code="a1b2c3", compact_limit=0, lanes={}, autonomy="delegate"
    )
    task = {
        "id": "t1",
        "title": "x",
        "handoff": "saved handoff",
        "handoff_envelope": {"reason": "quota", "seat": "eng-2@sw", "launch": saved},
    }
    with pytest.raises(
        SpawnError, match=f"^no claude or codex account can take the quota handoff from {harness} account old: "
    ) as error:
        runtime.spawn(config, "eng", "engineer@a1b2c3-0001", task)
    assert error.value.status == "unavailable"
    assert task["handoff"] == "saved handoff"
    assert task["handoff_envelope"]["launch"] == saved
    assert runtime._quota_accounts[0].sessions == 0
