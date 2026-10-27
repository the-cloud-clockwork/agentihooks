import json
import re
from pathlib import Path

import pytest

from scripts import cli_delegates
from scripts.headless import cli
from scripts.headless.profile_run import ProfileResult, ProfileRun

pytestmark = pytest.mark.unit

SCHEMA = {"type": "object", "required": ["verdict"]}


@pytest.fixture
def captured(monkeypatch, tmp_path):
    runs: list[ProfileRun] = []
    result = {"value": ProfileResult("ok", {"verdict": "pass"}, tmp_path / "t.jsonl", "s-1", 0)}

    def fake_run_profile(run: ProfileRun) -> ProfileResult:
        runs.append(run)
        return result["value"]

    monkeypatch.setattr(cli.profile_run, "run_profile", fake_run_profile)
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path / "home"))
    return runs, result


def test_the_cli_delegates_headless_to_this_module():
    assert cli_delegates.DELEGATED_CLIS["headless"] == "scripts.headless.cli"
    assert cli_delegates.delegated_cli(["headless", "run"]) is cli.main


def test_run_passes_every_flag_to_run_profile_and_prints_the_result_as_json(captured, tmp_path, capsys):
    runs, _ = captured
    schema = tmp_path / "schema.json"
    schema.write_text(json.dumps(SCHEMA))
    out = tmp_path / "out"

    code = cli.main(
        [
            "run",
            "engineer",
            "--input",
            "say hi",
            "--schema",
            str(schema),
            "--harness",
            "codex",
            "--out",
            str(out),
            "--tier",
            "low",
            "--effort",
            "high",
            "--timeout",
            "90",
        ]
    )

    assert code == 0
    assert runs == [ProfileRun("engineer", "codex", "say hi", SCHEMA, "low", "high", 90.0, out)]
    assert json.loads(capsys.readouterr().out) == {
        "status": "ok",
        "output": {"verdict": "pass"},
        "session": "s-1",
        "exit_code": 0,
        "transcript": str(tmp_path / "t.jsonl"),
        "run_dir": str(out),
    }


def test_run_defaults_harness_tier_effort_timeout_schema_and_a_named_run_folder(captured, tmp_path):
    runs, _ = captured

    assert cli.main(["run", "engineer", "--input", "hello"]) == 0

    (run,) = runs
    assert (run.profile, run.harness, run.input, run.schema) == ("engineer", "auto", "hello", None)
    assert (run.tier, run.effort, run.timeout) == ("high", "medium", 1800.0)
    assert run.run_dir.parent == tmp_path / "home" / "headless"
    assert re.fullmatch(r"\d{8}T\d{6}Z-[0-9a-f]{8}", run.run_dir.name)


def test_two_runs_get_different_folders(captured):
    runs, _ = captured

    cli.main(["run", "engineer", "--input", "a"])
    cli.main(["run", "engineer", "--input", "b"])

    assert runs[0].run_dir != runs[1].run_dir


def test_run_reads_the_input_from_a_file_when_the_value_names_one(captured, tmp_path):
    runs, _ = captured
    prompt = tmp_path / "prompt.md"
    prompt.write_text("from a file\n")

    cli.main(["run", "engineer", "--input", str(prompt)])

    assert runs[0].input == "from a file\n"


def test_run_keeps_a_directory_value_as_text(captured, tmp_path):
    runs, _ = captured

    cli.main(["run", "engineer", "--input", str(tmp_path)])

    assert runs[0].input == str(tmp_path)


@pytest.mark.parametrize("status", ["failed", "timeout", "invalid"])
def test_run_exits_one_when_the_run_is_not_ok(captured, tmp_path, capsys, status):
    _, result = captured
    result["value"] = ProfileResult(status, None, tmp_path / "t.jsonl", None, None)

    assert cli.main(["run", "engineer", "--input", "x", "--out", str(tmp_path / "o")]) == 1
    printed = json.loads(capsys.readouterr().out)
    assert (printed["status"], printed["output"], printed["session"], printed["exit_code"]) == (status, None, None, None)


def test_run_refuses_an_unknown_harness(captured, capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["run", "engineer", "--input", "x", "--harness", "gemini"])

    assert exit_info.value.code == 2
    assert "invalid choice: 'gemini'" in capsys.readouterr().err


def test_run_refuses_a_schema_file_that_is_not_json(captured, tmp_path, capsys):
    runs, _ = captured
    schema = tmp_path / "schema.json"
    schema.write_text("not json")

    assert cli.main(["run", "engineer", "--input", "x", "--schema", str(schema)]) == 1
    assert runs == []
    assert capsys.readouterr().err.startswith(f"agentihooks headless: schema {schema} is not JSON: ")


def test_run_refuses_a_missing_schema_file(captured, tmp_path, capsys):
    runs, _ = captured
    missing = tmp_path / "nope.json"

    assert cli.main(["run", "engineer", "--input", "x", "--schema", str(missing)]) == 1
    assert runs == []
    assert capsys.readouterr().err == f"agentihooks headless: no schema file {missing}\n"


def test_run_requires_an_input(captured):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["run", "engineer"])

    assert exit_info.value.code == 2


def test_default_home_falls_back_to_the_user_home(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENTIHOOKS_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    assert cli.default_run_dir({}).parent == tmp_path / ".agentihooks" / "headless"
