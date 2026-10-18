import io
import json
import os
import subprocess
import sys
import urllib.request

import pytest

from scripts.swarm import cli, naming, swarm_init
from scripts.swarm_ledger import ledger_creator
from tests.swarm.test_cli import env as env
from tests.swarm.test_create_gate import as_lane
from tests.swarm.test_stack_init import Docker, page
from tests.swarm.test_stack_init import home as home

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]

TITLE = "One way to create a swarm, with an operator given name"
RULE = (
    "  Rule: a swarm name is one or two lowercase words joined by a dash and ending in swarm, such as "
    "rig-grade-swarm or dating-swarm.\n"
)
PAUSED = "Swarm demo-swarm is paused; start it only when you want it: agentihooks swarm demo-swarm start\n"


@pytest.mark.parametrize("name", ["rig-grade-swarm", "dating-swarm", "swarm-swarm"])
def test_the_rule_takes_one_or_two_lowercase_words_ending_in_swarm(name):
    assert naming.swarm_name_problem(name) == ""


@pytest.mark.parametrize(
    "name",
    ["swarm", "demo", "-swarm", "Demo-swarm", "demo_swarm", "demo--swarm", "one-two-three-swarm", "demo-swarm-2"],
)
def test_the_rule_refuses_every_other_name(name):
    assert naming.swarm_name_problem(name) == f"{name} breaks the swarm name rule"


def test_a_missing_or_taken_name_says_so():
    assert naming.swarm_name_problem("") == "a swarm needs a name"
    assert naming.swarm_name_problem("rig-grade-swarm", {"rig-grade-swarm"}) == "rig-grade-swarm is taken"


def test_suggestions_come_from_the_plan_title_and_skip_taken_names():
    assert naming.swarm_name_suggestions(TITLE) == ["one-way-swarm", "one-swarm"]
    assert naming.swarm_name_suggestions(TITLE, {"one-way-swarm"}) == ["one-swarm", "way-swarm"]
    assert naming.swarm_name_suggestions("The plan for a swarm") == ["new-swarm", "next-swarm"]
    assert naming.swarm_name_suggestions("Dating app 2", count=3) == ["dating-app-swarm", "dating-swarm", "app-swarm"]


def test_only_proof_and_demo_slugs_are_code_named_swarms():
    assert naming.is_code_named_swarm("proof-a1b2c3-t1-1")
    assert naming.is_code_named_swarm("doctor-demo-20261006093000123456")
    assert not any(map(naming.is_code_named_swarm, ["demo-swarm", "rig-grade-swarm", "plan-2026-10-18", ""]))


@pytest.fixture
def plan(tmp_path):
    path = tmp_path / "one-way.md"
    path.write_text(f"intro\n# {TITLE}\n\n## Context\n")
    return path


@pytest.fixture
def scratch(env, home, monkeypatch, tmp_path):
    as_lane(monkeypatch, "operator", tmp_path / "scratch")
    monkeypatch.setenv("AGENTIHOOKS_SWARM_REDIS_URL", "redis://127.0.0.1:6390/0")
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(home))
    made = []

    class Ledgers:
        def create(self, slug, content, size):
            made.append((slug, content, size))
            return True

    monkeypatch.setattr(swarm_init, "new_ledger", Ledgers)
    monkeypatch.setattr(swarm_init, "taken", lambda store: set(store.slugs()))
    return env[0], made


def init(*argv, docker=None):
    out = io.StringIO()
    code = swarm_init.init(list(argv), os.environ, docker or Docker(), page, out)
    return code, out.getvalue()


@pytest.mark.parametrize("given", [[], ["Demo"]])
def test_init_without_a_valid_name_refuses_with_the_rule_and_two_suggestions(scratch, plan, given):
    docker = Docker()
    code, out = init(*given, "--plan", str(plan), docker=docker)
    problem = "a swarm needs a name" if not given else "Demo breaks the swarm name rule"
    assert (code, docker.calls) == (1, [])
    assert out == f"Swarm not created: {problem}.\n{RULE}  Try: one-way-swarm or one-swarm\n"


def test_init_brings_up_the_stack_then_creates_the_paused_swarm_and_its_ledger(scratch, plan, tmp_path):
    store, made = scratch
    docker = Docker()
    code, out = init("demo-swarm", "--plan", str(plan), "--repo", str(tmp_path), docker=docker)
    assert code == 0
    assert any("up" in call for call in docker.calls)
    phase = {"title": TITLE, "description": "", "planning": "auto"}
    assert made == [
        ("demo-swarm", {"title": TITLE, "overview": "", "sources": [str(plan)], "phases": [phase]}, "swarm")
    ]
    assert store.config("demo-swarm").state == "paused" and store.config("demo-swarm").repo == str(tmp_path)
    assert "  3. Start the swarm only when you want it: agentihooks swarm demo-swarm start\n" in out
    assert out.endswith(PAUSED)


def test_init_twice_is_safe_and_prints_the_swarm_again(scratch, plan, tmp_path):
    store, made = scratch
    assert init("demo-swarm", "--plan", str(plan), "--repo", str(tmp_path))[0] == 0
    code, out = init("demo-swarm", "--plan", str(plan), "--repo", str(tmp_path))
    assert (code, len(made), store.slugs()) == (0, 1, ["demo-swarm"])
    assert json.dumps({"swarm": "demo-swarm", "created": False, "state": "paused"}) + "\n" in out
    code, out = init("demo-swarm")
    assert (code, len(made)) == (0, 1) and out.endswith(PAUSED)


def test_a_name_taken_by_a_swarm_on_another_repo_is_refused(scratch, plan, tmp_path):
    store, made = scratch
    assert init("demo-swarm", "--plan", str(plan), "--repo", str(tmp_path))[0] == 0
    code, out = init("demo-swarm", "--repo", "/elsewhere")
    assert (code, len(made)) == (1, 1)
    assert out.endswith(f"Swarm not created: demo-swarm is taken by the swarm working {tmp_path}.\n")


def test_init_refuses_before_writing_a_ledger_when_this_session_may_not_create(scratch, plan, monkeypatch):
    store, made = scratch
    as_lane(monkeypatch, "eng")
    code, out = init("demo-swarm", "--plan", str(plan))
    assert (code, made, store.slugs()) == (1, [], [])
    assert out.endswith(f"Swarm not created: {ledger_creator.CALLER}.\n")


def test_a_content_file_seeds_the_ledger_and_the_suggestions(scratch, tmp_path):
    content = {"title": "Dating app", "phases": [{"title": "Build"}]}
    (tmp_path / "content.json").write_text(json.dumps(content))
    code, out = init("--content", str(tmp_path / "content.json"))
    assert code == 1 and out.endswith("  Try: dating-app-swarm or dating-swarm\n")
    assert init("dating-swarm", "--content", str(tmp_path / "content.json"))[0] == 0
    assert scratch[1] == [("dating-swarm", content, "swarm")]


def test_names_check_says_valid_or_invalid_with_the_reason_and_suggestions(scratch, plan, capsys):
    store, _ = scratch
    assert cli.main(["names", "check", "demo-swarm"]) == 0
    assert capsys.readouterr().out == "valid: demo-swarm\n"
    assert cli.main(["names", "check", "demo", "--plan", str(plan)]) == 1
    assert (
        capsys.readouterr().out
        == f"invalid: demo breaks the swarm name rule\n{RULE}  Try: one-way-swarm or one-swarm\n"
    )


def test_names_check_refuses_a_taken_name(scratch, plan, tmp_path, capsys):
    assert init("demo-swarm", "--plan", str(plan), "--repo", str(tmp_path))[0] == 0
    capsys.readouterr()
    assert cli.main(["names", "check", "demo-swarm"]) == 1
    assert capsys.readouterr().out == f"invalid: demo-swarm is taken\n{RULE}  Try: new-swarm or next-swarm\n"


def test_a_typed_create_is_refused_and_names_swarm_init(monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(cli, "main", lambda argv: seen.append(argv) or 0)
    assert cli.typed(["demo-swarm", "create", "--repo", "/repo"]) == 1
    assert capsys.readouterr().err == (
        "swarm: a swarm is created with its ledger by one command: "
        "agentihooks swarm init <name> --plan <file> --repo <dir>\n"
    )
    for argv in (["proof-a1b2c3-t1-1", "create", "--repo", "/r"], ["demo-swarm", "start"], ["list"]):
        assert cli.typed(argv) == 0
    assert seen == [["proof-a1b2c3-t1-1", "create", "--repo", "/r"], ["demo-swarm", "start"], ["list"]]


def test_init_and_names_check_route_from_the_swarm_command(monkeypatch):
    seen = []
    monkeypatch.setattr(swarm_init, "main", lambda argv: seen.append(("init", argv)) or 7)
    monkeypatch.setattr(swarm_init, "check", lambda argv: seen.append(("check", argv)) or 5)
    assert cli.main(["init", "demo-swarm", "--timeout", "9"]) == 7
    assert cli.main(["names", "check", "demo-swarm"]) == 5
    assert seen == [("init", ["demo-swarm", "--timeout", "9"]), ("check", ["demo-swarm"])]


def test_main_runs_init_with_the_process_environment_docker_and_stdout(monkeypatch):
    seen = []
    monkeypatch.setattr(swarm_init, "init", lambda *args: seen.append(args) or 3)
    assert swarm_init.main(["demo-swarm"]) == 3
    assert seen == [(["demo-swarm"], os.environ, subprocess.run, urllib.request.urlopen, sys.stdout)]
