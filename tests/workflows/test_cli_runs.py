import json
from unittest.mock import Mock

import pytest
import yaml

from scripts.workflows import cli, loader, schema, store
from scripts.workflows.runner import StageResult

DOCUMENT = {
    "workflow": {"name": "pair", "intent": "Build and check", "head": "build", "exit": "check.pass"},
    "stages": {
        "build": {"profile": "engineer", "next": {"pass": "check"}},
        "check": {"profile": "engineer", "input": ["build"], "next": {"pass": "exit"}},
    },
}


@pytest.fixture
def setup_run(monkeypatch, tmp_path):
    definition = schema.parse(yaml.safe_dump(DOCUMENT))
    root = tmp_path / "runs"
    case = tmp_path / "case.txt"
    case.write_text("Build the sample")
    monkeypatch.setattr(store, "default_root", lambda: root)
    monkeypatch.setattr(loader, "resolve", lambda cwd=None: ({"pair": loader.Found("project", case, definition)}, []))
    launch = Mock(return_value=StageResult("pass", [{"verdict": "pass"}]))
    return root, case, launch


def test_run_persists_two_stages_and_selected_harness(setup_run, monkeypatch, capsys):
    root, case, launch = setup_run
    launcher = Mock(launch=launch)
    factory = Mock(return_value=launcher)
    monkeypatch.setattr(cli, "HeadlessProfile", factory, raising=False)

    assert cli.main(["run", "pair", "--case", str(case), "--run-id", "sample", "--harness", "codex"]) == 0

    result = json.loads(capsys.readouterr().out)
    assert result == store.load(root / "sample")
    assert result["status"] == "passed"
    assert result["run"] == "sample"
    assert result["runs"] == {"build": 1, "check": 1}
    factory.assert_called_once_with(harness="codex")
    assert [call.args[0].id for call in launch.call_args_list] == ["build", "check"]
    assert launch.call_args_list[0].args[1] == {"case": "Build the sample"}
    assert launch.call_args_list[1].args[1] == {"case": "Build the sample", "build": [{"verdict": "pass"}]}
    assert all(call.args[2] == root / "sample" for call in launch.call_args_list)


def test_status_only_reads_saved_run(setup_run, monkeypatch, capsys):
    root, case, _ = setup_run
    definition = schema.parse(yaml.safe_dump(DOCUMENT))
    expected = store.create(root / "saved", definition, case.read_text())
    factory = Mock(side_effect=AssertionError("status launched a profile"))
    monkeypatch.setattr(cli, "HeadlessProfile", factory, raising=False)

    assert cli.main(["status", "saved"]) == 0

    assert json.loads(capsys.readouterr().out) == expected
    assert store.load(root / "saved") == expected
    factory.assert_not_called()


def test_resume_continues_saved_stage_and_original_definition(setup_run, monkeypatch, capsys):
    root, case, launch = setup_run
    launch.side_effect = [StageResult("pass", [{"verdict": "pass"}]), KeyboardInterrupt]
    factory = Mock(return_value=Mock(launch=launch))
    monkeypatch.setattr(cli, "HeadlessProfile", factory, raising=False)
    with pytest.raises(KeyboardInterrupt):
        cli.main(["run", "pair", "--case", str(case), "--run-id", "resume", "--harness", "claude"])
    assert store.load(root / "resume")["stage"] == "check"
    case.unlink()
    monkeypatch.setattr(loader, "resolve", Mock(side_effect=AssertionError("resume resolved current workflows")))
    launch.reset_mock(side_effect=True)
    launch.return_value = StageResult("pass", [{"verdict": "pass"}])

    assert cli.main(["resume", "resume"]) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "passed"
    assert result["runs"] == {"build": 1, "check": 1}
    assert launch.call_count == 1
    assert launch.call_args.args[0].id == "check"
    assert factory.call_args.kwargs == {"harness": "claude"}
    launch.reset_mock()
    assert cli.main(["resume", "resume"]) == 0
    assert json.loads(capsys.readouterr().out) == result
    launch.assert_not_called()


def test_run_allocates_identifier_and_defaults_harness(setup_run, monkeypatch, capsys):
    root, case, launch = setup_run
    factory = Mock(return_value=Mock(launch=launch))
    monkeypatch.setattr(cli, "HeadlessProfile", factory, raising=False)
    assert cli.main(["run", "pair", "--case", str(case)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert store.load(root / result["run"]) == result
    assert result["run"]
    factory.assert_called_once_with(harness="auto")


@pytest.mark.parametrize("action", ["status", "resume"])
def test_missing_run_is_a_json_error(setup_run, capsys, action):
    assert cli.main([action, "absent"]) == 1
    assert json.loads(capsys.readouterr().out)["error"]


@pytest.mark.parametrize("identifier", ["", "../escape", "/absolute", ".", "..", "nested/run"])
def test_commands_refuse_run_paths(setup_run, capsys, identifier):
    _, case, _ = setup_run
    assert cli.main(["run", "pair", "--case", str(case), "--run-id", identifier]) == 1
    assert json.loads(capsys.readouterr().out) == {"error": "run identifier must be a single name"}
    assert cli.main(["status", identifier]) == 1
    assert json.loads(capsys.readouterr().out) == {"error": "run identifier must be a single name"}


def test_run_refuses_unknown_workflow_as_json(setup_run, capsys):
    _, case, _ = setup_run
    assert cli.main(["run", "absent", "--case", str(case)]) == 1
    assert "absent" in json.loads(capsys.readouterr().out)["error"]


def test_run_refuses_unreadable_case_as_json(setup_run, capsys):
    _, case, _ = setup_run
    case.unlink()
    assert cli.main(["run", "pair", "--case", str(case)]) == 1
    assert json.loads(capsys.readouterr().out)["error"]


def test_run_never_overwrites_an_existing_run(setup_run, monkeypatch, capsys):
    root, case, launch = setup_run
    monkeypatch.setattr(cli, "HeadlessProfile", Mock(return_value=Mock(launch=launch)), raising=False)
    args = ["run", "pair", "--case", str(case), "--run-id", "same"]
    assert cli.main(args) == 0
    capsys.readouterr()
    expected = store.load(root / "same")
    assert cli.main(args) == 1
    assert json.loads(capsys.readouterr().out)["error"]
    assert store.load(root / "same") == expected


def test_run_reports_broken_workflow_layers_as_json(setup_run, monkeypatch, capsys):
    root, case, launch = setup_run
    errors = ["first workflow is invalid", "second workflow is invalid"]
    monkeypatch.setattr(loader, "resolve", lambda cwd=None: ({}, errors))
    monkeypatch.setattr(cli, "HeadlessProfile", Mock(return_value=Mock(launch=launch)))
    assert cli.main(["run", "pair", "--case", str(case)]) == 1
    assert json.loads(capsys.readouterr().out) == {"error": "\n".join(errors)}
    assert not root.exists()
    launch.assert_not_called()


@pytest.mark.parametrize(
    "arguments",
    [
        ["run", "pair"],
        ["run", "pair", "--case", "case", "--harness", "unknown"],
    ],
)
def test_run_requires_case_and_a_supported_harness(arguments):
    with pytest.raises(SystemExit) as caught:
        cli.parser().parse_args(arguments)
    assert caught.value.code == 2


def test_run_accepts_explicit_auto_harness():
    args = cli.parser().parse_args(["run", "pair", "--case", "case", "--harness", "auto"])
    assert args.harness == "auto"


def test_run_uses_the_exit_check_for_workflow_verdicts(setup_run, monkeypatch, capsys):
    _, case, launch = setup_run
    document = {**DOCUMENT, "workflow": {**DOCUMENT["workflow"], "exit": "all stages accepted"}}
    definition = schema.parse(yaml.safe_dump(document))
    monkeypatch.setattr(loader, "resolve", lambda: ({"pair": loader.Found("project", case, definition)}, []))
    judge = Mock(return_value=True)
    monkeypatch.setattr(cli.exit_checks, "check", judge)
    monkeypatch.setattr(cli, "HeadlessProfile", Mock(return_value=Mock(launch=launch)))
    assert cli.main(["run", "pair", "--case", str(case)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "passed"
    judge.assert_called_once_with("all stages accepted", {"build": [{"verdict": "pass"}]})


def test_exhausted_run_returns_failure_with_saved_json(setup_run, monkeypatch, capsys):
    root, case, launch = setup_run
    launch.return_value = StageResult("fail", [])
    document = {
        **DOCUMENT,
        "stages": {
            "build": {"profile": "engineer", "next": {"pass": "check", "fail": "exit"}},
            "check": DOCUMENT["stages"]["check"],
        },
    }
    definition = schema.parse(yaml.safe_dump(document))
    monkeypatch.setattr(loader, "resolve", lambda: ({"pair": loader.Found("project", case, definition)}, []))
    monkeypatch.setattr(cli, "HeadlessProfile", Mock(return_value=Mock(launch=launch)))
    assert cli.main(["run", "pair", "--case", str(case)]) == 1
    state = json.loads(capsys.readouterr().out)
    assert state["status"] == "exhausted"
    assert store.load(root / state["run"]) == state
