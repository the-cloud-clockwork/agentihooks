from pathlib import Path

import pytest
import yaml
from scripts.workflows import cli, loader, schema

from scripts import cli_delegates

TEXT = (
    "workflow:\n"
    "  name: tiny\n"
    "  intent: Build it.\n"
    "  head: build\n"
    "  exit: build.pass\n"
    "stages:\n"
    "  build:\n"
    "    profile: engineer\n"
    "    next:\n"
    "      pass: exit\n"
)


@pytest.fixture
def resolved(monkeypatch, tmp_path):
    path = tmp_path / "tiny.yaml"
    path.write_text(TEXT)
    found = {"tiny": loader.Found("project", path, schema.parse(TEXT))}
    errors = [f"{tmp_path / 'bad.yaml'}: stages.build: unknown key persona"]
    cwds = []

    def resolve(cwd=None):
        cwds.append(cwd)
        return found, list(errors)

    monkeypatch.setattr(cli.loader, "resolve", resolve)
    return {"path": path, "errors": errors, "cwds": cwds}


def test_the_cli_delegates_workflow_to_this_module():
    assert cli_delegates.DELEGATED_CLIS["workflow"] == "scripts.workflows.cli"
    assert cli_delegates.delegated_cli(["workflow", "list"]) is cli.main


def test_list_prints_each_workflow_with_its_layer_and_reports_broken_files(resolved, capsys):
    code = cli.main(["list"])

    out, err = capsys.readouterr()
    assert code == 1
    assert out == f"tiny\tproject\t{resolved['path']}\n"
    assert err == f"{resolved['errors'][0]}\n"
    assert resolved["cwds"] == [Path.cwd()]


def test_list_exits_zero_when_every_file_loads(resolved, capsys):
    resolved["errors"].clear()

    assert cli.main(["list"]) == 0
    assert capsys.readouterr().err == ""


def test_show_prints_the_workflow_with_defaults_filled_in(resolved, capsys):
    code = cli.main(["show", "tiny"])

    out = capsys.readouterr().out
    assert code == 0
    assert out.startswith(f"# project: {resolved['path']}\n")
    document = yaml.safe_load(out)
    assert document["workflow"]["max_iterations"] == 3
    assert document["stages"]["build"]["next"] == {"pass": "exit", "retry": None, "enhance": None}
    assert "{" not in out.split("\n", 1)[1]


def test_show_refuses_an_unknown_name(resolved, capsys):
    assert cli.main(["show", "nope"]) == 1
    assert capsys.readouterr().err == "agentihooks workflow: no workflow named nope\n"


def test_validate_accepts_a_valid_file(tmp_path, capsys):
    path = tmp_path / "w.yaml"
    path.write_text(TEXT)

    assert cli.main(["validate", str(path)]) == 0
    assert capsys.readouterr().out == f"valid: tiny ({path})\n"


def test_validate_refuses_a_stage_that_breaks_the_primitive(tmp_path, capsys):
    path = tmp_path / "w.yaml"
    path.write_text(TEXT.replace("    profile: engineer\n", "    profile: engineer\n    model: opus\n"))

    assert cli.main(["validate", str(path)]) == 1
    assert capsys.readouterr().err == f"{path}: stages.build: unknown key model\n"


def test_validate_accepts_a_name_that_loads(resolved, capsys):
    assert cli.main(["validate", "tiny"]) == 0
    assert capsys.readouterr().out == f"valid: tiny ({resolved['path']})\n"


def test_validate_refuses_a_name_that_is_not_a_file_or_a_workflow(resolved, capsys):
    assert cli.main(["validate", "nope"]) == 1
    assert capsys.readouterr().err == "agentihooks workflow: no workflow named nope\n"


def test_an_unknown_action_is_a_usage_error():
    with pytest.raises(SystemExit) as caught:
        cli.main(["run"])

    assert caught.value.code == 2
