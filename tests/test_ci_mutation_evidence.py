import json
from pathlib import Path
from unittest.mock import patch

import pytest


def _evidence(tmp_path):
    output = tmp_path / "gate"
    group = output / "0-work"
    group.mkdir(parents=True)
    row = {"name": "scripts.sample.x_value__mutmut_1", "status": "survived", "diff": "- return 1\n+ return 2\n"}
    report = {"files": [{"path": "scripts/sample.py", "rows": [row]}], "failed": True}
    (output / "report.json").write_text(json.dumps(report))
    selection = {"scripts/sample.py": {"lines": [2], "tests": ["tests/test_sample.py"]}}
    (group / "changed-lines.json").write_text(json.dumps(selection))
    for name in ("shared-stats.json", "shard-plan.json", "results.json"):
        (group / name).write_text("{}")
    (group / "run.log").write_text("survived\n")
    (group / "report.log").write_text("report complete\n")
    (group / "pyproject.toml").write_text("[tool.mutmut]\n")
    for name in (
        "scripts/sample.py",
        "tests/test_sample.py",
        "mutants/sample.py",
        "__pycache__/cache.pyc",
        "killers/cache.json",
    ):
        path = group / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("discard")
    return output, report


def test_bundle_keeps_failure_evidence_and_excludes_copies_and_caches(tmp_path):
    from scripts.ci_mutation.evidence import stage

    output, report = _evidence(tmp_path)
    bundle = tmp_path / "bundle"
    with patch("scripts.ci_mutation.evidence.subprocess.check_output", return_value="revision\n"):
        manifest = stage(tmp_path, output, bundle, (1, 3))
    assert json.loads((bundle / "report.json").read_text()) == report
    assert {p.relative_to(bundle).as_posix() for p in bundle.rglob("*") if p.is_file()} == {
        "report.json",
        "replay.json",
        "0-work/changed-lines.json",
        "0-work/shared-stats.json",
        "0-work/shard-plan.json",
        "0-work/results.json",
        "0-work/run.log",
        "0-work/report.log",
        "0-work/pyproject.toml",
    }
    assert manifest["head"] == "revision"
    assert manifest["groups"][0]["commands"] == [
        ["python", "-m", "scripts.ci_mutation.selection", "changed-lines.json", "1", "3", "reuse", "shared-stats.json"],
        ["python", "-m", "scripts.ci_mutation.report", "results.json", "1", "3", "scripts/sample.py"],
    ]
    assert json.loads((bundle / "replay.json").read_text()) == manifest


def test_bundle_preserves_every_shard_report_for_the_existing_aggregator(tmp_path):
    from scripts.ci_mutation.evidence import stage

    for shard in range(2):
        output = tmp_path / f"gate-{shard}"
        output.mkdir()
        (output / "report.json").write_text(json.dumps({"files": [], "not_mutated": [], "failed": False}))
        with patch("scripts.ci_mutation.evidence.subprocess.check_output", return_value="revision\n"):
            stage(tmp_path, output, tmp_path / "shards" / f"mutation-preflight-shard-{shard}", (shard, 2))
    from scripts.ci_mutation.merge import main

    assert main([str(tmp_path / "shards"), "--shards", "2", "--output", str(tmp_path / "merged")]) == 0
    (tmp_path / "shards/mutation-preflight-shard-1/report.json").unlink()
    assert main([str(tmp_path / "shards"), "--shards", "2", "--output", str(tmp_path / "merged")]) == 1


def test_successful_shard_keeps_only_the_report_and_revision(tmp_path):
    from scripts.ci_mutation.evidence import stage

    output, report = _evidence(tmp_path)
    report["failed"] = False
    (output / "report.json").write_text(json.dumps(report))
    with patch("scripts.ci_mutation.evidence.subprocess.check_output", return_value="revision\n"):
        stage(tmp_path, output, tmp_path / "bundle", (0, 1))
    assert {p.name for p in (tmp_path / "bundle").iterdir()} == {"report.json", "replay.json"}


def test_incomplete_bundle_keeps_logs_without_fabricating_a_report(tmp_path):
    from scripts.ci_mutation.evidence import stage

    output, _ = _evidence(tmp_path)
    (output / "report.json").unlink()
    with patch("scripts.ci_mutation.evidence.subprocess.check_output", return_value="revision\n"):
        stage(tmp_path, output, tmp_path / "bundle", (0, 1))
    assert not (tmp_path / "bundle/report.json").exists()
    assert (tmp_path / "bundle/0-work/run.log").read_text() == "survived\n"


def test_replay_rebuilds_sources_then_runs_the_recorded_commands(tmp_path):
    from scripts.ci_mutation.evidence import replay, stage

    output, _ = _evidence(tmp_path)
    bundle = tmp_path / "bundle"
    with patch("scripts.ci_mutation.evidence.subprocess.check_output", return_value="revision\n"):
        manifest = stage(tmp_path, output, bundle, (1, 3))
        with (
            patch("scripts.ci_mutation.evidence.prepare_workspace") as prepare,
            patch("scripts.ci_mutation.evidence.subprocess.run") as run,
        ):
            replay(tmp_path, bundle, tmp_path / "replayed", "0-work")
    prepare.assert_called_once_with(tmp_path, tmp_path / "replayed", ["scripts/sample.py"], ["tests/test_sample.py"])
    assert [call.args[0][1:] for call in run.call_args_list] == [cmd[1:] for cmd in manifest["groups"][0]["commands"]]
    assert all(call.kwargs["cwd"] == tmp_path / "replayed" and call.kwargs["check"] for call in run.call_args_list)
    assert (tmp_path / "replayed/shard-plan.json").read_text() == "{}"
    with patch("scripts.ci_mutation.evidence.subprocess.check_output", return_value="wrong\n"):
        with pytest.raises(ValueError, match="revision"):
            replay(tmp_path, bundle, tmp_path / "other", "0-work")


def test_shard_workflow_uploads_only_the_staged_bundle():
    import yaml

    root = Path(__file__).resolve().parents[1]
    job = yaml.safe_load((root / ".github/workflows/mutation-preflight.yml").read_text())["jobs"]["mutation-shard"]
    steps = {step.get("name"): step for step in job["steps"]}
    stage = steps["Stage shard replay evidence"]
    assert stage["if"] == "always()"
    assert "scripts.ci_mutation.evidence stage" in stage["run"]
    upload = steps["Upload shard mutation evidence"]
    assert "--output .mutation-workspaces --bundle .mutation-gate" in stage["run"]
    assert "mv .mutation-gate .mutation-workspaces" in stage["run"]
    assert upload["with"]["path"] == (
        "${{ job.status == 'success' && '.mutation-gate/report.json' || '.mutation-gate/' }}"
    )
    assert list(steps).index(stage["name"]) < list(steps).index(upload["name"])


@pytest.mark.parametrize(
    "argv, expected",
    [
        (["stage", "--shard", "2", "--shards", "7"], (Path(".mutation-gate"), Path(".mutation-evidence"), (2, 7))),
        (
            ["stage", "--output", "input", "--bundle", "archive", "--shard", "1", "--shards", "4"],
            (Path("input"), Path("archive"), (1, 4)),
        ),
    ],
)
def test_cli_stages_default_and_explicit_locations(tmp_path, monkeypatch, argv, expected):
    from scripts.ci_mutation.evidence import main

    monkeypatch.chdir(tmp_path)
    with patch("scripts.ci_mutation.evidence.stage") as stage:
        assert main(argv) == 0
    stage.assert_called_once_with(tmp_path, *expected)


def test_cli_replays_the_requested_group(tmp_path, monkeypatch):
    from scripts.ci_mutation.evidence import main

    monkeypatch.chdir(tmp_path)
    with patch("scripts.ci_mutation.evidence.replay") as replay:
        assert main(["replay", "--bundle", "archive", "--work", "restore", "--group", "2-worker"]) == 0
    replay.assert_called_once_with(tmp_path, Path("archive"), Path("restore"), "2-worker")


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["stage", "--shards", "2"],
        ["stage", "--shard", "0"],
        ["stage", "--shard", "bad", "--shards", "2"],
        ["stage", "--shard", "0", "--shards", "bad"],
        ["replay", "--work", "restore", "--group", "worker"],
        ["replay", "--bundle", "archive", "--group", "worker"],
        ["replay", "--bundle", "archive", "--work", "restore"],
    ],
)
def test_cli_rejects_incomplete_or_invalid_replay_requests(argv):
    from scripts.ci_mutation.evidence import main

    with pytest.raises(SystemExit) as error:
        main(argv)
    assert error.value.code == 2


def test_cli_help_explains_replay_prerequisites(capsys):
    from scripts.ci_mutation.evidence import main

    with pytest.raises(SystemExit) as error:
        main(["replay", "--help"])
    assert error.value.code == 0
    description = capsys.readouterr().out.split("\n\n")[1]
    assert " ".join(description.split()) == (
        "Check out replay.json's head and install its dependencies before replaying a group."
    )


def test_stage_skips_unrelated_entries_and_reports_complete_size(tmp_path, monkeypatch, capsys):
    from scripts.ci_mutation.evidence import stage

    output, _ = _evidence(tmp_path)
    (output / "!file").write_text("unrelated")
    (output / "!empty").mkdir()
    logs = output / "!logs"
    logs.mkdir()
    (logs / "run.log").write_text("setup failed")
    bundle = tmp_path / "nested" / "bundle"
    bundle.mkdir(parents=True)
    values = {"CI": "true", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONUNBUFFERED": "1"}
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    with patch("scripts.ci_mutation.evidence.subprocess.check_output", return_value="revision\n") as git:
        manifest = stage(tmp_path, output, bundle, (1, 3))
    git.assert_called_once_with(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True)
    assert manifest["env"] == values
    assert [entry["name"] for entry in manifest["groups"]] == ["0-work"]
    assert (bundle / "!logs/run.log").read_text() == "setup failed"
    files = [path for path in bundle.rglob("*") if path.is_file()]
    assert capsys.readouterr().out == (
        f"Mutation evidence: {len(files)} files, {sum(path.stat().st_size for path in files)} bytes\n"
    )


def test_replay_preserves_environment_and_stops_on_command_failure(tmp_path, monkeypatch):
    import os
    import subprocess
    import sys

    from scripts.ci_mutation.evidence import replay, stage

    output, _ = _evidence(tmp_path)
    monkeypatch.setenv("CI", "recorded")
    bundle = tmp_path / "bundle"
    with patch("scripts.ci_mutation.evidence.subprocess.check_output", return_value="revision\n"):
        manifest = stage(tmp_path, output, bundle, (1, 3))
    monkeypatch.setenv("CI", "current")
    work = tmp_path / "nested" / "replayed"
    with (
        patch("scripts.ci_mutation.evidence.subprocess.check_output", return_value="revision\n") as git,
        patch("scripts.ci_mutation.evidence.prepare_workspace"),
        patch(
            "scripts.ci_mutation.evidence.subprocess.run", side_effect=subprocess.CalledProcessError(1, "mutation")
        ) as run,
    ):
        with pytest.raises(subprocess.CalledProcessError):
            replay(tmp_path, bundle, work, "0-work")
    git.assert_called_once_with(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True)
    run.assert_called_once_with(
        [sys.executable, *manifest["groups"][0]["commands"][0][1:]],
        cwd=work,
        env=os.environ | {"CI": "recorded"},
        check=True,
    )
    assert (work / "results.json").read_text() == "{}"
