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
    assert upload["with"]["path"] == ".mutation-evidence/"
    assert list(steps).index(stage["name"]) < list(steps).index(upload["name"])
