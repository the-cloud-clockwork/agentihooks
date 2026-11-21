"""Pattern downloads leave a lone matched artifact where the step that reads it looks."""

import json
import re
import shlex
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
import yaml

from scripts import ci_gate_summary
from scripts.ci_lab.expressions import interpolate
from scripts.ci_mutation import merge, replay

ROOT = Path(__file__).resolve().parents[1]
GITHUB = ROOT / ".github"
SHARDS = re.compile(r"\bneeds\.[\w-]+\.outputs\.shards\b")
GREEN = {"files": [], "not_mutated": [], "failed": False}
MUTATION = ("workflows/test.yml", "mutation", "mutation-report-*")
REPLAY = ("workflows/mutation-replay.yml", "verify", "mutation-replay-grade-*")
LONE_FOLDER = {MUTATION, REPLAY}
ROOT_READ = {("workflows/test.yml", "test-summary", "{durations,flakes}-3.*")}
UNAFFECTED = {
    ("workflows/hierarchy-cpu.yml", "grade", "load-pair-*"),
    ("workflows/hierarchy-cpu.yml", "grade", "hierarchy-pair-*"),
    ("workflows/ledger-load.yml", "compare-grade", "compare-pair-*"),
    ("workflows/test.yml", "shard-check", "{durations,flakes}-${{ matrix.python-version }}-*"),
    ("workflows/test.yml", "sonar", "{reused-branch,reused}-3.12"),
    ("workflows/test.yml", "dep-manifest", "coverage-3.12-*"),
    ("workflows/test.yml", "coverage-ratchet", "executed-lines-3.12-*"),
    ("workflows/test.yml", "coverage-ratchet", "coverage-3.12-*"),
    ("workflows/test.yml", "refresh-durations", "durations-3.*"),
    ("workflows/test.yml", "flake-budget", "flakes-3.*"),
    ("workflows/test.yml", "coverage-baseline", "executed-lines-3.12-*"),
}


def _jobs(path: Path) -> list[tuple[str, list[dict[str, Any]]]]:
    """Return each job's steps in a workflow, or the steps of a composite action under the name runs."""
    document = yaml.safe_load(path.read_text())
    if not isinstance(document, dict):
        return []
    jobs = [
        (name, job.get("steps") or []) for name, job in (document.get("jobs") or {}).items() if isinstance(job, dict)
    ]
    runs = document.get("runs")
    return jobs + ([("runs", runs.get("steps") or [])] if isinstance(runs, dict) else [])


def _pattern_downloads() -> dict[tuple[str, str, str], dict[str, Any]]:
    """Return every pattern download under .github keyed by file, job and pattern; a retried attempt shares its key."""
    found: dict[tuple[str, str, str], dict[str, Any]] = {}
    for path in sorted([*GITHUB.rglob("*.yml"), *GITHUB.rglob("*.yaml")]):
        for job, steps in _jobs(path):
            for step in steps:
                settings = step.get("with") or {}
                if str(step.get("uses", "")).startswith("actions/download-artifact@") and "pattern" in settings:
                    found.setdefault((path.relative_to(GITHUB).as_posix(), job, settings["pattern"]), step)
    return found


def _steps(file: str, job: str) -> dict[str, dict[str, Any]]:
    """Return one job's named steps by name."""
    steps = dict(_jobs(GITHUB / file))[job]
    return {step["name"]: step for step in steps if "name" in step}


def _matches(name: str, pattern: str) -> bool:
    """Return whether an artifact name matches a download pattern, expanding brace groups as minimatch does."""
    braces = re.fullmatch(r"([^{]*)\{([^{}]*)\}(.*)", pattern)
    if braces is None:
        return fnmatch(name, pattern)
    head, options, tail = braces.groups()
    return any(_matches(name, head + option + tail) for option in options.split(","))


def _download(
    step: dict[str, Any], artifacts: dict[str, dict[str, str]], workspace: Path, contexts: dict[str, str]
) -> None:
    """Lay out the artifacts a pattern download matches the way actions/download-artifact v8.0.2 extracts them.

    The release extracts into the bare path when the pattern matches exactly one artifact or merge-multiple is set,
    and into a folder named after each artifact when it matches several.
    """
    assert step["uses"] == "actions/download-artifact@v8.0.2"
    settings = step["with"]
    path = workspace / interpolate(settings["path"], contexts)
    matched = [name for name in sorted(artifacts) if _matches(name, settings["pattern"])]
    for name in matched:
        folder = path if len(matched) == 1 or settings.get("merge-multiple") is True else path / name
        for relative, text in artifacts[name].items():
            (folder / relative).parent.mkdir(parents=True, exist_ok=True)
            (folder / relative).write_text(text)


@pytest.mark.feature("pattern-downloads-keep-a-lone-artifact-where-its-reader-looks")
def test_every_pattern_download_without_merge_multiple_says_where_a_lone_match_lands() -> None:
    unmerged = {key for key, step in _pattern_downloads().items() if step["with"].get("merge-multiple") is not True}
    assert not LONE_FOLDER & ROOT_READ
    assert not (LONE_FOLDER | ROOT_READ) & UNAFFECTED
    assert unmerged == LONE_FOLDER | ROOT_READ | UNAFFECTED


@pytest.mark.feature("pattern-downloads-keep-a-lone-artifact-where-its-reader-looks")
@pytest.mark.parametrize("key", sorted(LONE_FOLDER))
def test_a_lone_shard_download_lands_in_a_folder_its_pattern_names(key: tuple[str, str, str]) -> None:
    path = _pattern_downloads()[key]["with"]["path"]
    outputs = set(SHARDS.findall(path))
    lone = PurePosixPath(interpolate(path, dict.fromkeys(outputs, "[0]")))
    shared = PurePosixPath(interpolate(path, dict.fromkeys(outputs, "[0, 1]")))
    assert outputs
    assert lone.parent == shared
    assert fnmatch(lone.name, key[2])


@pytest.mark.feature("pattern-downloads-keep-a-lone-artifact-where-its-reader-looks")
@pytest.mark.parametrize("count", [1, 2, 3])
@pytest.mark.parametrize("source", ["mutation-plan", "mutation-refine", "mutation-replan"])
def test_the_mutation_merge_finds_every_shard_report_however_many_shards_ran(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], count: int, source: str
) -> None:
    order = ["mutation-replan", "mutation-refine", "mutation-plan"]
    lower = order[order.index(source) + 1 :]
    contexts = {f"needs.{job}.outputs.shards": "[0, 1, 2, 3]" if job in lower else "" for job in order}
    contexts[f"needs.{source}.outputs.shards"] = json.dumps(list(range(count)))
    steps = _steps(MUTATION[0], MUTATION[1])
    merging = steps["Merge the shard reports"]
    shards = json.loads(interpolate(merging["env"]["SHARDS"], contexts))
    reports = {
        f"mutation-report-{shard}": {
            "report.json": json.dumps({**GREEN, "shard": {"index": shard, "expected": 0, "graded": 0}})
        }
        for shard in shards
    }
    _download(steps["Download every mutation shard report"], reports, tmp_path, contexts)
    command = shlex.split(merging["run"].replace('"$(jq length <<< "$SHARDS")"', str(len(shards))))
    assert command[:3] == ["python", "-m", "scripts.ci_mutation.merge"]
    monkeypatch.chdir(tmp_path)
    assert merge.main(command[3:]) == 0
    assert f"Merged {count} of {count} mutation shard reports" in capsys.readouterr().out


@pytest.mark.feature("mutation-merge-defaults-to-the-prefix-the-tests-workflow-passes")
def test_the_mutation_merge_defaults_to_the_prefix_the_tests_workflow_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    command = shlex.split(_steps(MUTATION[0], MUTATION[1])["Merge the shard reports"]["run"])
    assert command[:3] == ["python", "-m", "scripts.ci_mutation.merge"]
    prefix = command[command.index("--prefix") + 1]
    report = tmp_path / f"{prefix}0" / "report.json"
    report.parent.mkdir()
    report.write_text(json.dumps({**GREEN, "shard": {"index": 0, "expected": 0, "graded": 0}}))
    argv = [str(tmp_path), "--shards", "1", "--output", str(tmp_path / "gate")]
    assert merge.main(argv) == 0, capsys.readouterr().out


@pytest.mark.feature("pattern-downloads-keep-a-lone-artifact-where-its-reader-looks")
@pytest.mark.parametrize("count", [1, 2])
def test_the_mutation_replay_verify_finds_every_grade_however_many_shards_ran(tmp_path: Path, count: int) -> None:
    assert _steps(REPLAY[0], REPLAY[1])["Verify complete grading and retained survivor failures"]["run"] == (
        "python -m scripts.ci_mutation.replay verify"
    )
    folder = tmp_path / ".mutation-replay"
    (folder / "stats").mkdir(parents=True)
    (folder / "manifest.json").write_text(json.dumps({"case": "second"}))
    plan = {"shards": count, "assignments": {f"mutant_{shard}": shard for shard in range(count)}}
    (folder / "stats" / "plan.json").write_text(json.dumps(plan))
    measured = {"expected": 1, "graded": 1, "complete": True, "budget_exhausted": False}
    grades = {
        f"mutation-replay-grade-{shard}": {"report.json": json.dumps({**GREEN, "shard": {"index": shard, **measured}})}
        for shard in range(count)
    }
    contexts = {"needs.plan.outputs.shards": json.dumps(list(range(count)))}
    _download(_pattern_downloads()[REPLAY], grades, tmp_path, contexts)
    proof = replay.verify(folder)
    assert proof["complete"] is True
    assert [shard["index"] for shard in proof["shards"]] == list(range(count))


@pytest.mark.feature("pattern-downloads-keep-a-lone-artifact-where-its-reader-looks")
@pytest.mark.parametrize("legs", [1, 2])
def test_the_gate_summary_counts_every_leg_however_many_legs_ran(tmp_path: Path, legs: int) -> None:
    reports: dict[str, dict[str, str]] = {}
    for leg in range(1, legs + 1):
        reports[f"durations-3.12-{leg}"] = {"durations.json": json.dumps({"test_ok": 1})}
        reports[f"flakes-3.12-{leg}"] = {"flakes.jsonl": json.dumps({"test": "test_retry", "outcome": "flaky"}) + "\n"}
    downloads = _pattern_downloads()
    for key in sorted(ROOT_READ):
        _download(downloads[key], reports, tmp_path, {})
    folder = shlex.split(_steps("workflows/test.yml", "test-summary")["Summarize this run's tests"]["run"])[2]
    assert ci_gate_summary.counts(tmp_path / folder) == {"ran": 2 * legs, "flaky": legs, "failed": 0}


@pytest.mark.feature("pattern-downloads-keep-a-lone-artifact-where-its-reader-looks")
def test_the_gate_summary_counts_a_lone_report_that_lands_at_the_folder_root(tmp_path: Path) -> None:
    reports = {"durations-3.12-1": {"durations.json": json.dumps({"test_ok": 1})}}
    downloads = _pattern_downloads()
    for key in sorted(ROOT_READ):
        _download(downloads[key], reports, tmp_path, {})
    folder = shlex.split(_steps("workflows/test.yml", "test-summary")["Summarize this run's tests"]["run"])[2]
    assert (tmp_path / folder / "durations.json").is_file()
    assert ci_gate_summary.counts(tmp_path / folder) == {"ran": 1, "flaky": 0, "failed": 0}
