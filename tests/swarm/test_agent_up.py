from types import SimpleNamespace

import pytest

from hooks.context import context_recycle, profile_chain
from scripts.gates import Who
from scripts.swarm import agent_up, prompt
from scripts.swarm.runtime import HerdrRuntime
from scripts.swarm.store import SwarmError
from scripts.swarm.tick import Placed
from tests.swarm.profile_fixture import validated
from tests.swarm.test_cli import env, run  # noqa: F401

pytestmark = pytest.mark.xdist_group("fakeredis")
ROLES = {"planner": "planner", "engineer": "engineer", "qa": "qa", "cicd": "cicd", "frontend": "engineer"}


@pytest.fixture
def roles(monkeypatch):
    found = {**ROLES, "master": "master", "bare": ""}
    monkeypatch.setattr(agent_up, "_role", lambda profile: found.get(profile))


@pytest.mark.parametrize(
    ("profile", "role", "lane"),
    [
        ("planner", "planner", "plan"),
        ("engineer", "engineer", "eng"),
        ("qa", "qa", "eng"),
        ("cicd", "cicd", "ci"),
        ("frontend", "engineer", "eng"),
    ],
)
def test_a_role_or_overlay_profile_resolves_to_its_base_role_and_name_lane(roles, profile, role, lane):
    assert agent_up.resolve(profile) == agent_up.Resolved(profile, role, lane)


@pytest.mark.parametrize(
    ("profile", "message"),
    [
        ("master", "the master comes online with agentihooks swarm <id> master up"),
        ("ghost", "profile ghost is not installed: install it with agentihooks init"),
        ("bare", "profile bare wears no base role among planner, engineer, qa, cicd"),
    ],
)
def test_a_master_missing_or_roleless_profile_is_refused(roles, profile, message):
    with pytest.raises(SwarmError) as caught:
        agent_up.resolve(profile)
    assert str(caught.value) == message


def test_the_role_of_an_installed_profile_comes_from_its_chain(monkeypatch):
    chains = {
        "qa-plus": [
            ("anton-base", profile_chain.PACKAGE_ROLES.parent / "x"),
            ("qa", profile_chain.PACKAGE_ROLES / "qa"),
        ],
        "flat": [("flat", profile_chain.PACKAGE_ROLES.parent / "flat")],
    }
    installer = SimpleNamespace(
        _resolve_profile_dir=lambda name: "dir" if name in chains else None,
        _resolve_profile_chain=lambda name: chains[name],
    )
    monkeypatch.setattr("scripts.targets._common._install_module", lambda: installer)
    assert (agent_up._role("qa-plus"), agent_up._role("flat"), agent_up._role("none")) == ("qa", "", None)


class OperatorRuntime:
    def __init__(self, fail=""):
        self.launched, self.reaped, self.fail = [], [], fail

    def operator(self, config, name, profile, text):
        if self.fail:
            raise RuntimeError(self.fail)
        self.launched.append((config.slug, name, profile, text))
        return Placed(pane_id="w1:p7", harness="claude")

    def reap_name(self, name):
        self.reaped.append(name)
        return True


@pytest.fixture
def swarm(env, roles, tmp_path):  # noqa: F811
    store, _, _ = env
    run("sw", "create", "--repo", str(tmp_path), "--max-eng-agents", "0", "--max-ci-agents", "0")
    return store


def test_up_names_the_agent_from_code_marks_it_operator_and_claims_nothing(swarm):
    rt = OperatorRuntime()
    launched = agent_up.up(swarm, "sw", rt, "planner", 1000)
    assert launched == agent_up.Launched("planner@a1b2c3-0001", "w1:p7", "planner", "planner")
    assert rt.launched[0][:3] == ("sw", "planner@a1b2c3-0001", "planner")
    assert rt.launched[0][3] == prompt.build_operator(
        "sw", swarm.config("sw").repo, "planner@a1b2c3-0001", "planner", "planner"
    )
    entry = swarm.names.entry("planner@a1b2c3-0001")
    assert (entry["operator"], entry["swarm"], entry["spawned_at"], entry["retired_at"]) == ("planner", "sw", 1000, 0)
    assert swarm.agents("sw") == []


def test_up_numbers_each_launch_of_a_kind_in_turn(swarm):
    rt = OperatorRuntime()
    names = [agent_up.up(swarm, "sw", rt, profile, 1000).name for profile in ("frontend", "engineer", "cicd")]
    assert names == ["engineer@a1b2c3-0001", "engineer@a1b2c3-0002", "ci@a1b2c3-0001"]


def test_a_failed_launch_retires_its_name_and_says_why(swarm):
    with pytest.raises(SwarmError) as caught:
        agent_up.up(swarm, "sw", OperatorRuntime(fail="no account has room"), "qa", 2000)
    assert str(caught.value) == "engineer@a1b2c3-0001 could not start: no account has room"
    assert swarm.names.entry("engineer@a1b2c3-0001")["retired_at"] == 2000


def test_cli_profile_up_launches_and_prints_the_launch(swarm, monkeypatch, capsys):
    from scripts.swarm import cli

    rt = OperatorRuntime()
    monkeypatch.setattr(cli, "HerdrRuntime", lambda: rt)
    capsys.readouterr()
    assert run("sw", "planner", "up") == 0
    assert capsys.readouterr().out.strip() == (
        '{"name": "planner@a1b2c3-0001", "pane": "w1:p7", "profile": "planner", "role": "planner"}'
    )
    assert rt.launched[0][1] == "planner@a1b2c3-0001"


def test_cli_master_up_still_brings_the_master(swarm, monkeypatch):
    from scripts.swarm import cli

    seen = []
    monkeypatch.setattr(cli, "cmd_master", lambda store, args: seen.append((args.command, args.action)))
    assert run("sw", "master", "up") == 0
    assert seen == [("master", "up")]


def test_cli_profile_up_refuses_extra_arguments(swarm):
    with pytest.raises(SystemExit):
        run("sw", "planner", "up", "--now")


def test_exit_retires_an_operator_name_then_ends_its_session(swarm, monkeypatch, capsys):
    from scripts.swarm import cli

    rt = OperatorRuntime()
    monkeypatch.setattr(cli, "HerdrRuntime", lambda: rt)
    agent_up.up(swarm, "sw", rt, "planner", 1000)
    monkeypatch.setattr(cli, "now_ms", lambda: 5000)
    monkeypatch.setattr(cli, "Who", SimpleNamespace(from_env=lambda: Who(name="planner@a1b2c3-0001", swarm="sw")))
    capsys.readouterr()
    assert run("sw", "exit") == 0
    assert capsys.readouterr().out.strip() == '{"exited": "planner@a1b2c3-0001"}'
    assert swarm.names.entry("planner@a1b2c3-0001")["retired_at"] == 5000
    assert rt.reaped == ["planner@a1b2c3-0001"]


@pytest.mark.parametrize("name", ["engineer@a1b2c3-0001", ""])
def test_exit_refuses_a_name_the_operator_did_not_launch(swarm, name):
    swarm.next_name("sw", "eng", 1)
    with pytest.raises(SwarmError) as caught:
        agent_up.retire(swarm, "sw", name, 5000)
    assert str(caught.value) == f"{name or 'this session'} was not launched with agentihooks swarm sw <profile> up"
    assert swarm.names.entry("engineer@a1b2c3-0001")["retired_at"] == 0


def test_exit_refuses_an_operator_name_of_another_swarm(swarm):
    agent_up.up(swarm, "sw", OperatorRuntime(), "planner", 1000)
    with pytest.raises(SwarmError) as caught:
        agent_up.retire(swarm, "other", "planner@a1b2c3-0001", 5000)
    assert str(caught.value) == "planner@a1b2c3-0001 was not launched with agentihooks swarm other <profile> up"


def _operator_launch(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTIHOOKS_COMPACT_LIMIT", raising=False)
    seen = {}

    def run_(argv, **kwargs):
        seen["argv"], seen["env"] = argv, kwargs.get("env")
        out = "status=started\nroute_status=routed\npane_id=w1:p7\n"
        return SimpleNamespace(returncode=0, stdout=validated(argv, out), stderr="")

    runtime = HerdrRuntime(home=tmp_path, run=run_, choose=lambda *_: ("claude", "open"))
    config = SimpleNamespace(
        slug="sw", repo=str(tmp_path), code="a1b2c3", compact_limit=0, lanes={}, autonomy="delegate"
    )
    placed = runtime.operator(config, "planner@a1b2c3-0001", "planner", "hello")
    return placed, seen


def test_the_operator_launch_binds_the_swarm_with_no_task_and_its_own_lane(tmp_path, monkeypatch):
    placed, seen = _operator_launch(tmp_path, monkeypatch)
    bound = tuple(seen["env"][key] for key in ("AGENTIHOOKS_SWARM", "AGENTIHOOKS_SWARM_LANE", "AGENTIHOOKS_SWARM_TASK"))
    assert bound == ("sw", "operator", "")
    assert placed.pane_id == "w1:p7"


def test_the_operator_launch_opens_claude_in_the_swarm_space_with_the_inbox_channel(tmp_path, monkeypatch):
    _, seen = _operator_launch(tmp_path, monkeypatch)
    argv = seen["argv"]
    prompt_file = tmp_path / "sw" / "prompts" / "planner@a1b2c3-0001.md"
    assert argv[1 : argv.index("--")] == [
        "init-agent",
        "--host",
        "herdr",
        "--workspace",
        f"{tmp_path.name}-a1b2c3",
        "--dir",
        str(tmp_path),
        "--name",
        "planner@a1b2c3-0001",
        "--agent",
        "claude",
        "--start-timeout",
        "30",
        "--route-timeout",
        "90",
        "--inbox-channel",
        "--profile",
        "planner",
        "--prompt-file",
        str(prompt_file),
    ]
    assert "--route" not in argv and "--permission-mode" not in argv
    assert prompt_file.read_text() == "hello"


def test_the_operator_launch_starts_on_the_frontier_model(tmp_path, monkeypatch):
    from scripts.swarm import model_pick

    _, seen = _operator_launch(tmp_path, monkeypatch)
    after = seen["argv"][seen["argv"].index("--") + 1 :]
    assert after[after.index("--model") + 1] == model_pick.frontier("claude").model


@pytest.mark.parametrize(("lane", "slug"), [("operator", ""), ("plan", "sw"), ("", "sw")])
def test_the_context_recycle_gate_skips_an_operator_launched_agent(lane, slug):
    environ = {"AGENTIHOOKS_AGENT_NAME": "planner@a1b2c3-0001", "AGENTIHOOKS_SWARM": "sw"}
    assert context_recycle._swarm_of({**environ, "AGENTIHOOKS_SWARM_LANE": lane}) == slug


def test_the_planner_prompt_plans_with_the_operator_then_registers_the_plan_and_exits():
    text = prompt.build_operator("sw", "/repo", "planner@a1b2c3-0001", "planner", "planner")
    led = "agentihooks ledger --slug sw --as planner@a1b2c3-0001"
    assert text.splitlines() == [
        "You are planner@a1b2c3-0001, a planner profile agent the operator launched on swarm sw over the repo /repo "
        "with agentihooks swarm sw planner up. You hold no task and no lane slot, and the swarm never nudges or "
        "retires you. You answer to the operator in this pane: wait for his first message.",
        f"The swarm ledger is {prompt.ledger_path('sw')}. Read it for context; change nothing on it until the "
        "operator accepts a plan.",
        'Other sessions reach you as inbox messages: answer one with agentihooks msg reply <id> "<text>" and reach '
        'the master with agentihooks msg send master@sw "<text>".',
        "",
        "Plan with the operator here. Ask what you need, edit no code, and revise until he accepts the plan. On his "
        "accept, in this order:",
        "1. Write the plan as markdown and its phases as JSON, the init-swarm content phases shape with planning "
        "manual on each phase, in a folder from agentihooks scratch new.",
        f"2. Append the phases: {led} plan phases <phases file>. Note the phase ids it prints.",
        f"3. Publish the plan: {led} publish-plan <plan file> --phase <phase ids>. {prompt.PUBLISHED}, and links and "
        "comments each phase.",
        f"4. Add each phase's tasks: {led} task add - <title> --phase <id> --lane <eng or ci> --kind <kind> "
        '--description "<scope and Done when sentence>" --depends-on <ids> --territory <areas>. The ledger takes '
        "tasks only in phases you appended.",
        '5. Tell the master: agentihooks msg send master@sw "<the plan link, the phases and tasks you added, and that '
        'they wait on its review>".',
        "6. Tell the operator here what you registered, then end this session with agentihooks swarm sw --as "
        "planner@a1b2c3-0001 exit.",
    ]


def test_another_role_works_with_the_operator_and_exits_when_he_says_so():
    text = prompt.build_operator("sw", "/repo", "engineer@a1b2c3-0002", "engineer", "frontend")
    lines = text.splitlines()
    assert lines[0] == (
        "You are engineer@a1b2c3-0002, a frontend profile agent the operator launched on swarm sw over the repo /repo "
        "with agentihooks swarm sw frontend up. You hold no task and no lane slot, and the swarm never nudges or "
        "retires you. You answer to the operator in this pane: wait for his first message."
    )
    assert lines[1] == (
        f"The swarm ledger is {prompt.ledger_path('sw')}. Read it for context; change nothing on it unless the "
        "operator asks."
    )
    assert lines[4:] == [
        "Work with the operator on what he asks in this pane, through a worktree and a pull request into dev for any "
        "code change. Propose other work with agentihooks ledger --slug sw --as engineer@a1b2c3-0002 followup add "
        '"<plain words>".',
        "When he says you are done, end this session with agentihooks swarm sw --as engineer@a1b2c3-0002 exit.",
    ]
