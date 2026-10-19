import contextlib
import importlib
import io
import json
import os
import subprocess
import sys
import urllib.request
from unittest import mock

import pytest

from scripts.swarm import cli, naming, stack_env, stack_init, swarm_init
from scripts.swarm_ledger import ledger_creator, new_ledger
from tests.swarm.test_cli import env as env
from tests.swarm.test_create_gate import as_lane
from tests.swarm.test_stack_init import Docker, compose, page, up

pytestmark = pytest.mark.unit

TITLE = "One way to create a swarm, with an operator given name"
RULE = (
    "  Rule: a swarm name is one or two lowercase words joined by a dash and ending in swarm, such as "
    "rig-grade-swarm or dating-swarm.\n"
)
PAUSED = "Swarm demo-swarm is paused; start it only when you want it: agentihooks swarm demo-swarm start\n"
LINK = "Ledger page: http://127.0.0.1:8766/demo-swarm (open it to follow and steer the work)"


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
    held = {"one-two-swarm", "one-swarm", "two-swarm", "three-swarm"}
    assert naming.swarm_name_suggestions("one two three", held) == ["two-three-swarm", "new-swarm"]


def test_only_proof_and_demo_slugs_are_code_named_swarms():
    assert naming.is_code_named_swarm("proof-a1b2c3-t1-1")
    assert naming.is_code_named_swarm("doctor-demo-20261006093000123456")
    assert not any(map(naming.is_code_named_swarm, ["demo-swarm", "rig-grade-swarm", "plan-2026-10-18"]))


@pytest.fixture
def plan(tmp_path):
    path = tmp_path / "one-way.md"
    path.write_text(f"intro\n# {TITLE}\n\n## Context\n")
    return path


class Stack(Docker):
    """The stack's swarm container: an exec runs its script in this process, on the fixture's Redis and ledgers."""

    ledgers = None

    def __call__(self, argv, capture_output, text, input=None):
        if "exec" not in argv:
            return super().__call__(argv, capture_output, text)
        assert capture_output is True and text is True and isinstance(json.loads(input), dict)
        self.calls.append(argv[: argv.index("-c")])
        printed, code = io.StringIO(), 0
        inside = mock.patch.dict(os.environ, {"LEDGER_DIR": str(self.ledgers)})
        with inside, contextlib.redirect_stdout(printed), mock.patch.object(sys, "stdin", io.StringIO(input)):
            try:
                exec(argv[-1], {"__name__": "__main__"})
            except SystemExit as stop:
                code = stop.code or 0
        return subprocess.CompletedProcess(argv, code, printed.getvalue(), "")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(stack_init.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(stack_env, "free_port", lambda preferred: preferred + 1)
    return tmp_path / "agentihooks-home"


@pytest.fixture
def scratch(env, home, monkeypatch, tmp_path):
    ledgers = tmp_path / "stack-ledgers"
    ledgers.mkdir()
    as_lane(monkeypatch, "operator", ledgers)
    monkeypatch.setattr(importlib.import_module("ledger_core"), "LEDGER_DIR", ledgers)
    monkeypatch.setenv("AGENTIHOOKS_SWARM_REDIS_URL", "redis://127.0.0.1:6390/0")
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(home))
    monkeypatch.setattr(Stack, "ledgers", ledgers)
    return env[0]


def init(*argv, docker=None):
    out = io.StringIO()
    code = swarm_init.init(list(argv), os.environ, docker or Stack(), page, out)
    return code, out.getvalue()


@pytest.mark.parametrize("given", [[], ["Demo"]])
def test_init_without_a_valid_name_refuses_with_the_rule_and_two_suggestions(scratch, plan, given):
    docker = Stack()
    code, out = init(*given, "--plan", str(plan), docker=docker)
    problem = "a swarm needs a name" if not given else "Demo breaks the swarm name rule"
    assert (code, docker.calls) == (1, [])
    assert out == f"Swarm not created: {problem}.\n{RULE}  Try: one-way-swarm or one-swarm\n"


def test_init_brings_up_the_stack_then_creates_the_paused_swarm_and_its_ledger_in_it(scratch, home, plan, tmp_path):
    docker = Stack()
    code, out = init("demo-swarm", "--plan", str(plan), "--repo", str(tmp_path), docker=docker)
    assert code == 0
    started, made = up(home, "300"), compose(home, "exec", "-T", "-e", "LEDGER_HOST=127.0.0.1", "swarm", "python")
    assert started in docker.calls and docker.calls.index(started) < docker.calls.index(made)
    doc = new_ledger.storage.default().get_document("demo-swarm")
    assert (doc["size"], doc["title"], [p["title"] for p in doc["phases"]]) == ("swarm", TITLE, [TITLE])
    assert scratch.config("demo-swarm").state == "paused" and scratch.config("demo-swarm").repo == str(tmp_path)
    assert "  3. Start the swarm only when you want it: agentihooks swarm demo-swarm start\n" in out
    created = {"created": "demo-swarm", "repo": str(tmp_path), "state": "paused", "template": "", "scaling": "auto"}
    assert out.endswith(f"{json.dumps(created)}\n{LINK}\n{PAUSED}")


def test_init_twice_is_safe_and_prints_the_swarm_again(scratch, plan, tmp_path):
    assert init("demo-swarm", "--plan", str(plan), "--repo", str(tmp_path))[0] == 0
    code, out = init("demo-swarm", "--plan", str(plan), "--repo", str(tmp_path))
    assert (code, scratch.slugs()) == (0, ["demo-swarm"])
    assert json.dumps({"swarm": "demo-swarm", "created": False, "repo": str(tmp_path)}) + f"\n{LINK}\n" in out
    code, out = init("demo-swarm")
    assert code == 0 and out.endswith(f"{LINK}\n{PAUSED}")


def test_a_name_taken_by_a_swarm_on_another_repo_is_refused_with_the_rule(scratch, plan, tmp_path):
    assert init("demo-swarm", "--plan", str(plan), "--repo", str(tmp_path))[0] == 0
    code, out = init("demo-swarm", "--plan", str(plan), "--repo", "/elsewhere")
    assert (code, scratch.config("demo-swarm").repo) == (1, str(tmp_path))
    assert out.endswith(
        f"Swarm not created: demo-swarm is taken by the swarm working {tmp_path}.\n"
        f"{RULE}  Try: one-way-swarm or one-swarm\n"
    )


def test_a_name_held_by_a_ledger_that_is_not_a_swarm_is_refused(scratch, plan):
    new_ledger.create("demo-swarm", {"title": "Notes", "phases": [{"title": "One"}]}, "small")
    code, out = init("demo-swarm", "--plan", str(plan))
    assert (code, scratch.slugs()) == (1, [])
    assert out.endswith(
        f"Swarm not created: demo-swarm is taken by a ledger that is not a swarm's.\n{RULE}  Try: one-way-swarm or one-swarm\n"
    )
    assert new_ledger.storage.default().get_document("demo-swarm")["title"] == "Notes"


def test_init_refuses_before_the_stack_writes_when_this_session_may_not_create(scratch, plan, monkeypatch):
    as_lane(monkeypatch, "eng")
    docker = Stack()
    code, out = init("demo-swarm", "--plan", str(plan), docker=docker)
    assert (code, scratch.slugs(), [c for c in docker.calls if "exec" in c]) == (1, [], [])
    assert out.endswith(f"Swarm not created: {ledger_creator.CALLER}.\n")


def test_a_swarm_too_small_for_the_shared_folder_needs_the_operator_to_ask(scratch, plan, monkeypatch):
    as_lane(monkeypatch, "master")
    docker = Stack()
    code, out = init("demo-swarm", "--plan", str(plan), docker=docker)
    assert (code, [c for c in docker.calls if "exec" in c]) == (1, [])
    assert out.endswith(f"Swarm not created: {ledger_creator.FLOOR.format(need=3, have=1)}.\n")


def test_a_stack_failure_is_named(scratch, plan):
    class Broken(Stack):
        def __call__(self, argv, capture_output, text, input=None):
            if "exec" in argv:
                return subprocess.CompletedProcess(argv, 1, "", "Traceback\nRuntimeError: the ledger folder is full\n")
            return super().__call__(argv, capture_output, text)

    code, out = init("demo-swarm", "--plan", str(plan), docker=Broken())
    assert code == 1 and out.endswith(
        "Swarm not created: the stack refused it: RuntimeError: the ledger folder is full.\n"
    )


def test_a_content_file_seeds_the_ledger_and_the_suggestions_and_its_host_sources_stay_out(scratch, tmp_path):
    content = {"title": "Dating app", "sources": [str(tmp_path)], "phases": [{"title": "Build"}, {"title": "Ship"}]}
    (tmp_path / "content.json").write_text(json.dumps(content))
    code, out = init("--content", str(tmp_path / "content.json"))
    assert code == 1 and out.endswith("  Try: dating-app-swarm or dating-swarm\n")
    assert init("dating-swarm", "--content", str(tmp_path / "content.json"))[0] == 0
    doc = new_ledger.storage.default().get_document("dating-swarm")
    assert ([p["title"] for p in doc["phases"]], doc["sources"]) == (["Build", "Ship"], [])


def test_names_check_says_valid_or_invalid_with_the_reason_and_suggestions(scratch, plan, capsys):
    assert swarm_init.check(["demo-swarm"], os.environ, Stack()) == 0
    assert capsys.readouterr().out == "valid: demo-swarm\n"
    assert swarm_init.check(["demo", "--plan", str(plan)], os.environ, Stack()) == 1
    assert (
        capsys.readouterr().out
        == f"invalid: demo breaks the swarm name rule\n{RULE}  Try: one-way-swarm or one-swarm\n"
    )


def test_names_check_refuses_a_name_the_stack_holds(scratch, plan, tmp_path, capsys):
    assert init("demo-swarm", "--plan", str(plan), "--repo", str(tmp_path))[0] == 0
    new_ledger.create("next-swarm", {"title": "Notes", "phases": [{"title": "One"}]}, "small")
    capsys.readouterr()
    assert swarm_init.check(["demo-swarm"], os.environ, Stack()) == 1
    assert capsys.readouterr().out == f"invalid: demo-swarm is taken\n{RULE}  Try: new-swarm or work-swarm\n"


def test_names_check_with_the_stack_down_checks_the_rule_only(scratch, capsys):
    class Down(Stack):
        def __call__(self, argv, capture_output, text, input=None):
            return subprocess.CompletedProcess(argv, 1, "", "service swarm is not running")

    assert swarm_init.check(["demo-swarm"], os.environ, Down()) == 0
    assert capsys.readouterr().out == (
        "valid: demo-swarm; the swarm stack did not answer, so the names it holds were not checked\n"
    )


def test_a_swarm_ledger_left_without_its_swarm_is_free_and_init_finishes_it(scratch, plan, tmp_path, capsys):
    new_ledger.create("demo-swarm", {"title": "Earlier", "phases": [{"title": "One"}]}, "swarm")
    assert swarm_init.check(["demo-swarm"], os.environ, Stack()) == 0
    assert capsys.readouterr().out == "valid: demo-swarm\n"
    assert init("demo-swarm", "--plan", str(plan), "--repo", str(tmp_path))[0] == 0
    assert scratch.slugs() == ["demo-swarm"]
    assert new_ledger.storage.default().get_document("demo-swarm")["title"] == "Earlier"


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


def test_init_help_names_the_command_and_every_option():
    text = " ".join(swarm_init.parser().format_help().split())
    assert text.startswith("usage: agentihooks swarm init [-h] [--plan PLAN] [--content CONTENT] [--repo REPO]")
    for words in (
        "agentihooks swarm init NAME: bring up the swarm stack, then create the swarm and its ledger under NAME",
        "name the swarm name, which is also its ledger name",
        "--plan PLAN the accepted plan file; its title seeds the ledger and the name suggestions",
        "--content CONTENT ledger content JSON as ledger new takes it; default one phase for the plan",
        "--repo REPO the repository the swarm works; default the current directory",
        "--template TEMPLATE the swarm template for caps, models and lanes",
        "--operator-asked OPERATOR_ASKED the operator's words asking for a swarm with fewer than three tasks",
        "--timeout TIMEOUT seconds to wait for every service to be healthy",
        "--prepare-clients verify bundle credentials on existing local Redis and activate clients",
        "--adopt serve the existing shared ledger folder as this user from the stack, on the default ledger port, "
        "and keep doing so on every later run",
        "--no-start write the stack files and settings, adoption included, and stop: no service, swarm or ledger",
    ):
        assert words in text


def test_prepare_clients_needs_no_name_and_runs_only_the_stack_preparation(monkeypatch):
    seen = []
    monkeypatch.setattr(stack_init, "init", lambda *given: seen.append(given) or 3)
    out = io.StringIO()
    docker = Stack()
    assert swarm_init.init(["--prepare-clients"], {"A": "1"}, docker, page, out) == 3
    assert seen == [(["--prepare-clients"], {"A": "1"}, docker, page, out, "")]
    assert (docker.calls, out.getvalue()) == ([], "")


@pytest.mark.parametrize(("given", "passed"), [(["--adopt"], ["--adopt", "--no-start"]), ([], ["--no-start"])])
def test_no_start_needs_no_name_and_creates_no_swarm(monkeypatch, given, passed):
    seen = []
    monkeypatch.setattr(stack_init, "init", lambda *args: seen.append(args) or 0)
    out = io.StringIO()
    docker = Stack()
    assert swarm_init.init([*given, "--no-start"], {"A": "1"}, docker, page, out) == 0
    assert seen == [(passed, {"A": "1"}, docker, page, out, "")]
    assert (docker.calls, out.getvalue()) == ([], "")


def test_adopt_reaches_the_stack_before_the_swarm_is_created(monkeypatch, plan):
    seen = []
    monkeypatch.setattr(stack_init, "init", lambda *args: seen.append(args[0]) or 4)
    out = io.StringIO()
    assert swarm_init.init(["demo-swarm", "--adopt", "--plan", str(plan)], {}, Stack(), page, out) == 4
    assert seen == [["--timeout", "300", "--adopt"]]


def test_adopt_and_prepare_clients_are_refused_together(capsys):
    with pytest.raises(SystemExit) as raised:
        swarm_init.init(["--adopt", "--prepare-clients"], {}, Stack(), page, io.StringIO())
    assert raised.value.code == 2
    assert "argument --prepare-clients: not allowed with argument --adopt" in capsys.readouterr().err


def test_names_check_help_names_the_command_and_its_plan_option(capsys):
    with pytest.raises(SystemExit):
        swarm_init.check(["--help"], os.environ, Stack())
    text = " ".join(capsys.readouterr().out.split())
    assert text.startswith("usage: agentihooks swarm names check [-h] [--plan PLAN] name")
    assert "--plan PLAN the plan file whose title seeds the suggestions" in text
    assert "valid or invalid, the reason and suggestions" in text


def test_the_timeout_is_a_number_passed_to_the_stack(scratch, plan, tmp_path, capsys):
    with pytest.raises(SystemExit):
        init("demo-swarm", "--timeout", "soon")
    assert "invalid int value: 'soon'" in capsys.readouterr().err
    docker = Stack()
    assert init("demo-swarm", "--plan", str(plan), "--timeout", "9", docker=docker)[0] == 0
    assert any(call[-2:] == ["--wait-timeout", "9"] for call in docker.calls)


def test_a_plan_without_a_heading_is_titled_from_its_file_name(tmp_path):
    plan = tmp_path / "dating_app-launch.md"
    plan.write_text("no heading here\n")
    assert swarm_init.plan_title(str(plan)) == "dating app launch"


def test_with_no_plan_or_content_the_ledger_is_one_automatic_phase_named_after_the_swarm():
    args = swarm_init.parser().parse_args(["dating-app-swarm"])
    phase = {"title": "dating app swarm", "description": "", "planning": "auto"}
    assert swarm_init.seed(args) == {"title": "dating app swarm", "overview": "", "phases": [phase]}


def test_the_repo_defaults_to_the_directory_init_runs_in(scratch, plan, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert init("demo-swarm", "--plan", str(plan))[0] == 0
    assert scratch.config("demo-swarm").repo == str(tmp_path)


@pytest.mark.parametrize(("stdout", "said"), [("half written\n", "half written"), ("", "no output")])
def test_a_silent_stack_failure_names_its_last_output(scratch, plan, stdout, said):
    class Quiet(Stack):
        def __call__(self, argv, capture_output, text, input=None):
            if "exec" in argv:
                return subprocess.CompletedProcess(argv, 1, stdout, "")
            return super().__call__(argv, capture_output, text)

    code, out = init("demo-swarm", "--plan", str(plan), docker=Quiet())
    assert code == 1 and out.endswith(f"Swarm not created: the stack refused it: {said}.\n")


def test_init_with_nothing_given_refuses_with_fallback_suggestions(scratch):
    docker = Stack()
    code, out = init(docker=docker)
    assert (code, docker.calls) == (1, [])
    assert out == f"Swarm not created: a swarm needs a name.\n{RULE}  Try: new-swarm or next-swarm\n"


def test_suggestions_take_the_plan_title_over_the_content_title(scratch, plan, tmp_path):
    (tmp_path / "content.json").write_text(json.dumps({"title": "Dating app", "phases": [{"title": "Build"}]}))
    code, out = init("Demo", "--plan", str(plan), "--content", str(tmp_path / "content.json"))
    assert code == 1 and out.endswith("  Try: one-way-swarm or one-swarm\n")


def test_content_without_a_title_falls_back_to_the_fixed_suggestions(scratch, tmp_path):
    (tmp_path / "content.json").write_text(json.dumps({"phases": [{"title": "Build"}]}))
    code, out = init("Demo", "--content", str(tmp_path / "content.json"))
    assert code == 1 and out.endswith("  Try: new-swarm or next-swarm\n")


def test_refusals_are_red_on_a_terminal(scratch, plan, monkeypatch):
    class Terminal(io.StringIO):
        def isatty(self):
            return True

    out = Terminal()
    assert swarm_init.init(["Demo", "--plan", str(plan)], os.environ, Stack(), page, out) == 1
    assert out.getvalue().startswith("\033[1;31mSwarm not created: Demo breaks the swarm name rule.\033[0m\n")
    as_lane(monkeypatch, "eng")
    out = Terminal()
    assert swarm_init.init(["demo-swarm", "--plan", str(plan)], os.environ, Stack(), page, out) == 1
    assert out.getvalue().endswith(f"\033[1;31mSwarm not created: {ledger_creator.CALLER}.\033[0m\n")


def test_the_operator_asking_lifts_the_shared_folder_floor(scratch, plan, monkeypatch):
    as_lane(monkeypatch, "operator")
    assert init("demo-swarm", "--plan", str(plan))[0] == 1
    asked = init("demo-swarm", "--plan", str(plan), "--operator-asked", "make one small demo swarm")
    assert asked[0] == 0 and scratch.slugs() == ["demo-swarm"]


def test_a_template_reaches_the_swarm_create_in_the_stack(scratch, plan, monkeypatch):
    from scripts.swarm import templates

    monkeypatch.setattr(cli.templates, "load", lambda name, environ: templates.parse({"name": name}))
    assert init("demo-swarm", "--plan", str(plan), "--template", "codex-ci")[0] == 0
    assert scratch.config("demo-swarm").template == "codex-ci"


def test_a_taken_refusal_suggests_around_every_swarm_the_stack_holds(scratch, plan, tmp_path):
    assert init("one-way-swarm", "--plan", str(plan), "--repo", str(tmp_path))[0] == 0
    assert init("demo-swarm", "--plan", str(plan), "--repo", str(tmp_path))[0] == 0
    code, out = init("demo-swarm", "--plan", str(plan), "--repo", "/elsewhere")
    assert code == 1 and out.endswith("  Try: one-swarm or way-swarm\n")


def test_main_runs_init_with_the_process_environment_docker_and_stdout(monkeypatch):
    seen = []
    monkeypatch.setattr(swarm_init, "init", lambda *args: seen.append(args) or 3)
    assert swarm_init.main(["demo-swarm"]) == 3
    assert seen == [(["demo-swarm"], os.environ, subprocess.run, urllib.request.urlopen, sys.stdout)]
