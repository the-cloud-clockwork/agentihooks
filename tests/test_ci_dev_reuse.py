import hashlib
import json
import os
import re
import subprocess
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from scripts import ci_dev_reuse, ci_test_plan

HEAD = "a" * 40
TREE = "b" * 40
REPOSITORY = "the-cloud-clockwork/agentihooks"


EXPECTED_ARTIFACTS = set(
    """
split-3.12 collected-3.12
durations-3.12-1 flakes-3.12-1 coverage-3.12-1
durations-3.12-2 flakes-3.12-2 coverage-3.12-2
durations-3.12-3 flakes-3.12-3 coverage-3.12-3
durations-3.12-4 flakes-3.12-4 coverage-3.12-4
durations-3.12-5 flakes-3.12-5 coverage-3.12-5
durations-3.12-6 flakes-3.12-6 coverage-3.12-6
durations-3.12-7 flakes-3.12-7 coverage-3.12-7
durations-3.12-8 flakes-3.12-8 coverage-3.12-8
durations-3.12-9 flakes-3.12-9 coverage-3.12-9
durations-3.12-10 flakes-3.12-10 coverage-3.12-10
durations-3.12-11 flakes-3.12-11 coverage-3.12-11
durations-3.12-12 flakes-3.12-12 coverage-3.12-12
durations-3.12-13 flakes-3.12-13 coverage-3.12-13
durations-3.12-14 flakes-3.12-14 coverage-3.12-14
durations-3.12-15 flakes-3.12-15 coverage-3.12-15
durations-3.12-16 flakes-3.12-16 coverage-3.12-16
durations-3.12-17 flakes-3.12-17 coverage-3.12-17
durations-3.12-18 flakes-3.12-18 coverage-3.12-18
durations-3.12-19 flakes-3.12-19 coverage-3.12-19
durations-3.12-20 flakes-3.12-20 coverage-3.12-20
""".split()
)


def attest(folder, record, source=""):
    with zipfile.ZipFile(folder / "proof.zip", "w") as archive:
        archive.writestr("provenance.json", json.dumps(record))
    digest = hashlib.sha256(json.dumps(record, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    (folder / "reuse.log").write_text(
        f"2026-10-13T00:00:00Z required-tree-sha256={digest}\n2026-10-13T00:00:00Z run={source}\n"
    )


def shard_artifacts(shards: int) -> set[str]:
    legs = {name: re.fullmatch(r".+-3\.12-(\d+)", name) for name in EXPECTED_ARTIFACTS}
    return {name for name, leg in legs.items() if leg is None or int(leg[1]) <= shards}


def evidence(
    tmp_path: Path, *, reused: bool = False, run_id: int = 123, shards: int = 20
) -> tuple[dict[str, Any], dict[str, Any], Path]:
    run = {
        "id": run_id,
        "run_attempt": 2,
        "event": "merge_group",
        "head_sha": HEAD,
        "status": "completed",
        "conclusion": "success",
        "path": ".github/workflows/test.yml",
        "repository": {"full_name": REPOSITORY},
    }
    record = {
        "version": 1,
        "run": run_id,
        "attempt": 2,
        "event": "merge_group",
        "commit": HEAD,
        "tree": TREE,
        "reused": reused,
        "inputs": {"day": datetime.now(UTC).date().isoformat(), "workflow": "c" * 40},
    }
    folder = tmp_path / str(run_id)
    folder.mkdir()
    attest(folder, record)
    jobs = [{"name": "reuse", "conclusion": "success"}]
    jobs += [{"name": f"unit (3.12, {shard})", "conclusion": "success"} for shard in range(1, shards + 1)]
    (folder / "jobs.jsonl").write_text("".join(json.dumps(job) + "\n" for job in jobs))
    artifacts = [{"name": name, "expired": False} for name in sorted(shard_artifacts(shards))]
    (folder / "artifacts.jsonl").write_text("".join(json.dumps(item) + "\n" for item in artifacts))
    (tmp_path / "runs.json").write_text(json.dumps({"workflow_runs": [run]}))
    return run, record, folder


@pytest.mark.parametrize("producer", ["durations", "select", "lint", "unit (3.12, 1)"])
@pytest.mark.parametrize("result", ["success", "failure", "skipped"])
def test_queue_provenance_can_move_only_to_successful_durations(tmp_path, producer, result):
    _, _, folder = evidence(tmp_path)
    jobs = [json.loads(line) for line in (folder / "jobs.jsonl").read_text().splitlines()]
    jobs[0] = {"name": producer, "conclusion": result}
    (folder / "jobs.jsonl").write_text("".join(json.dumps(job) + "\n" for job in jobs))
    expected = 123 if producer in {"durations", "select"} and result == "success" else None
    assert ci_dev_reuse.select_source(tmp_path, HEAD, REPOSITORY) == expected


def test_exact_passed_queue_commit_reuses_all_unit_artifacts(tmp_path):
    evidence(tmp_path)
    assert ci_dev_reuse.select_source(tmp_path, HEAD, REPOSITORY) == 123


def test_queue_partitions_with_more_shards_do_not_reuse_a_partial_promotion_suite(tmp_path):
    _, _, folder = evidence(tmp_path)
    with (folder / "jobs.jsonl").open("a") as jobs:
        jobs.write(json.dumps({"name": "unit (3.12, 21)", "conclusion": "success"}) + "\n")

    assert ci_dev_reuse.select_source(tmp_path, HEAD, REPOSITORY) is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("head_sha", "d" * 40),
        ("event", "push"),
        ("status", "in_progress"),
        ("conclusion", "failure"),
        ("path", ".github/workflows/proofs.yml"),
    ],
)
def test_mismatched_or_unpassed_queue_runs_fall_back(tmp_path, field, value):
    run, _, _ = evidence(tmp_path)
    run[field] = value
    (tmp_path / "runs.json").write_text(json.dumps({"workflow_runs": [run]}))
    assert ci_dev_reuse.select_source(tmp_path, HEAD, REPOSITORY) is None


@pytest.mark.parametrize("kind", ["missing", "expired", "failed_unit", "unauthenticated", "old_day"])
def test_incomplete_evidence_falls_back(tmp_path, kind, capsys):
    _, record, folder = evidence(tmp_path)
    if kind == "missing":
        (folder / "artifacts.jsonl").write_text("")
    elif kind == "expired":
        items = [json.loads(line) for line in (folder / "artifacts.jsonl").read_text().splitlines()]
        items[0]["expired"] = True
        (folder / "artifacts.jsonl").write_text("".join(json.dumps(item) + "\n" for item in items))
    elif kind == "failed_unit":
        content = (folder / "jobs.jsonl").read_text().replace('"success"', '"failure"')
        (folder / "jobs.jsonl").write_text(content)
    elif kind == "unauthenticated":
        (folder / "reuse.log").write_text("")
    else:
        record["inputs"]["day"] = (datetime.now(UTC).date() - timedelta(days=1)).isoformat()
        attest(folder, record)
    assert ci_dev_reuse.select_source(tmp_path, HEAD, REPOSITORY) is None
    if kind == "old_day":
        assert capsys.readouterr().out == "No dev unit reuse from run 123: dependency day changed\n"


def test_missing_metadata_falls_back(tmp_path):
    assert ci_dev_reuse.select_source(tmp_path, HEAD, REPOSITORY) is None


@pytest.mark.parametrize("payload", [{"workflow_runs": None}, {"workflow_runs": [None]}, None, []])
def test_malformed_run_metadata_falls_back(tmp_path, payload):
    (tmp_path / "runs.json").write_text(json.dumps(payload))
    assert ci_dev_reuse.select_source(tmp_path, HEAD, REPOSITORY) is None


def test_malformed_run_entry_does_not_hide_a_passed_candidate(tmp_path):
    good, _, _ = evidence(tmp_path)
    (tmp_path / "runs.json").write_text(json.dumps({"workflow_runs": [None, good]}))
    assert ci_dev_reuse.select_source(tmp_path, HEAD, REPOSITORY) == 123


def test_null_provenance_inputs_fall_back(tmp_path):
    _, record, folder = evidence(tmp_path)
    record["inputs"] = None
    with zipfile.ZipFile(folder / "proof.zip", "w") as archive:
        archive.writestr("provenance.json", json.dumps(record))
    digest = hashlib.sha256(json.dumps(record, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    (folder / "reuse.log").write_text(f"2026-10-13T00:00:00Z required-tree-sha256={digest}\n")
    assert ci_dev_reuse.select_source(tmp_path, HEAD, REPOSITORY) is None


def test_cli_publishes_empty_source_on_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    evidence(tmp_path)
    output = tmp_path / "outputs"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert ci_dev_reuse.main(["--head", "d" * 40, "--repository", REPOSITORY, "--evidence", str(tmp_path)]) == 0
    assert output.read_text() == "source=\n"


def test_cli_publishes_passed_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    evidence(tmp_path)
    output = tmp_path / "outputs"
    output.write_text("existing=kept\n")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert ci_dev_reuse.main(["--head", HEAD, "--repository", REPOSITORY, "--evidence", str(tmp_path)]) == 0
    assert output.read_text() == "existing=kept\nsource=123\n"


@pytest.mark.parametrize("shards", [1, 12, 16, 19])
def test_a_queue_run_with_fewer_shards_feeds_the_dev_push(tmp_path: Path, shards: int) -> None:
    evidence(tmp_path, shards=shards)
    assert ci_dev_reuse.select_source(tmp_path, HEAD, REPOSITORY) == 123


@pytest.mark.parametrize("shards", [12, 16, 20])
def test_split_maps_every_push_leg_onto_a_queue_shard(shards: int) -> None:
    import yaml

    jobs = yaml.safe_load((Path(__file__).parents[1] / ".github/workflows/test.yml").read_text())["jobs"]
    step = next(step for step in jobs["split"]["steps"] if step.get("id") == "queue-legs")
    assert step["if"] == "needs.select.outputs.source != ''"
    assert jobs["split"]["outputs"]["queue_legs"] == "${{ steps.queue-legs.outputs.queue_legs }}"
    legs = re.search(r"jq -nc --argjson shards \"\$shards\" '([^']+)'", step["run"])
    assert legs is not None
    mapped = subprocess.run(
        ["jq", "-nc", "--argjson", "shards", str(shards), legs[1]], capture_output=True, text=True, check=True
    )
    assert json.loads(mapped.stdout) == {str(leg): (leg - 1) % shards + 1 for leg in range(1, 21)}


def test_a_queue_run_with_a_missing_middle_shard_never_feeds_the_dev_push(tmp_path: Path) -> None:
    _, _, folder = evidence(tmp_path, shards=16)
    jobs = [json.loads(line) for line in (folder / "jobs.jsonl").read_text().splitlines()]
    (folder / "jobs.jsonl").write_text(
        "".join(json.dumps(job) + "\n" for job in jobs if job["name"] != "unit (3.12, 9)")
    )
    assert ci_dev_reuse.select_source(tmp_path, HEAD, REPOSITORY) is None


def test_a_queue_run_whose_last_shard_failed_never_feeds_the_dev_push(tmp_path: Path) -> None:
    _, _, folder = evidence(tmp_path, shards=16)
    with (folder / "jobs.jsonl").open("a") as jobs:
        jobs.write(json.dumps({"name": "unit (3.12, 17)", "conclusion": "failure"}) + "\n")
    assert ci_dev_reuse.select_source(tmp_path, HEAD, REPOSITORY) is None


@pytest.mark.parametrize("missing", ["coverage-3.12-16", "durations-3.12-16", "flakes-3.12-16"])
def test_a_sixteen_shard_queue_run_missing_its_last_artifact_never_feeds_the_dev_push(
    tmp_path: Path, missing: str
) -> None:
    _, _, folder = evidence(tmp_path, shards=16)
    items = [json.loads(line) for line in (folder / "artifacts.jsonl").read_text().splitlines()]
    (folder / "artifacts.jsonl").write_text(
        "".join(json.dumps(item) + "\n" for item in items if item["name"] != missing)
    )
    assert ci_dev_reuse.select_source(tmp_path, HEAD, REPOSITORY) is None


def source_evidence(tmp_path, queue, record, folder):
    source = dict(queue, id=456, event="pull_request", head_sha="d" * 40)
    source_folder = tmp_path / "456"
    source_folder.mkdir()
    source_record = dict(record, run=456, event="pull_request", commit="e" * 40, reused=False)
    attest(source_folder, source_record)
    for name in ("jobs.jsonl", "artifacts.jsonl"):
        (source_folder / name).write_text((folder / name).read_text())
    (folder / "source.json").write_text(json.dumps(source))
    attest(folder, record, source="456")
    return source, source_record, source_folder


def test_queue_reused_pull_request_resolves_its_authenticated_artifacts(tmp_path):
    queue, record, folder = evidence(tmp_path, reused=True)
    source_evidence(tmp_path, queue, record, folder)
    assert ci_dev_reuse.select_source(tmp_path, HEAD, REPOSITORY) == 456


def test_source_from_other_repository_falls_back(tmp_path):
    run, _, _ = evidence(tmp_path)
    run["repository"]["full_name"] = "other/agentihooks"
    (tmp_path / "runs.json").write_text(json.dumps({"workflow_runs": [run]}))
    assert ci_dev_reuse.select_source(tmp_path, HEAD, REPOSITORY) is None


@pytest.mark.feature("dev-push-copies-reused-unit-results-in-one-job")
def test_workflow_preserves_downstream_artifacts() -> None:
    import yaml

    jobs = yaml.safe_load((Path(__file__).parents[1] / ".github/workflows/test.yml").read_text())["jobs"]
    unit = jobs["unit"]
    steps = {step.get("name"): step for step in unit["steps"]}
    assert steps["Run tests"]["if"] == "needs.split.outputs.source == ''"
    restored = steps["Restore every queue leg"]
    assert restored["with"]["run-id"] == "${{ needs.split.outputs.source }}"
    assert restored["with"]["pattern"] == "{durations,flakes,coverage}-${{ matrix.python-version }}-*"
    assert restored["with"]["path"] == "${{ runner.temp }}/queue-legs"
    laid = steps["Lay out every push leg from its queue leg"]
    assert laid["if"] == "needs.split.outputs.source != ''"
    assert laid["env"] == {"QUEUE_LEGS": "${{ needs.split.outputs.queue_legs }}"}
    assert laid["run"] == (
        'python -m scripts.ci_unit_copy layout --queue "$RUNNER_TEMP/queue-legs" --legs "$QUEUE_LEGS"'
        ' --out "$RUNNER_TEMP/push-legs"'
    )
    sent = steps["Upload every push leg"]
    assert sent["if"] == "needs.split.outputs.source != ''"
    assert sent["uses"] == "actions/github-script@v7"
    assert sent["env"] == {"PUSH_LEGS": "${{ runner.temp }}/push-legs"}
    assert sent["with"]["github-token"] == "none"
    assert (
        "'-m', 'scripts.ci_unit_copy', 'upload', '--out', process.env.PUSH_LEGS], "
        "{stdio: 'inherit', cwd: process.env.GITHUB_WORKSPACE}"
    ) in sent["with"]["script"]
    assert "queue_legs" not in jobs["select"]["outputs"]
    producer = "durations" if "durations" in jobs else "select"
    assert jobs["split"]["outputs"]["source"] == "${{ needs." + producer + ".outputs.source }}"
    assert jobs.get("durations", jobs["select"])["outputs"]["source"] == "${{ steps.unit-source.outputs.source }}"
    for job in ("coverage-baseline", "refresh-durations", "flake-budget"):
        assert "unit" in jobs[job]["needs"]
    assert any(step.get("name") == "Take each shard's coverage as it lands" for step in jobs["sonar"]["steps"])


@pytest.mark.feature("dev-push-copies-reused-unit-results-in-one-job")
def test_the_copy_job_sets_up_the_python_its_layout_runs_and_skips_every_leg_upload() -> None:
    import yaml

    jobs = yaml.safe_load((Path(__file__).parents[1] / ".github/workflows/test.yml").read_text())["jobs"]
    steps = {step.get("name"): step for step in jobs["unit"]["steps"]}
    names = list(steps)
    setup = [
        "Set up Python ${{ matrix.python-version }}",
        "Stamp the day",
        "Restore the test environment",
        "Set up uv",
        "Install dependencies",
        "Use the test environment",
    ]
    for name in setup:
        assert "UNIT_SOURCE" not in steps[name].get("if", "")
        assert "outputs.source" not in steps[name].get("if", "")
    assert names.index("Use the test environment") < names.index("Lay out every push leg from its queue leg")
    assert (
        names.index("Lay out every push leg from its queue leg")
        < names.index("Check and summarize every push leg's coverage")
        < names.index("Upload every push leg")
    )
    for name in (
        "Check the coverage database",
        "Upload coverage",
        "Summarize the executed lines",
        "Upload the executed lines summary",
    ):
        assert steps[name]["if"] == "needs.split.outputs.source == '' && matrix.python-version == '3.12'"
    for name in ("Record flaky tests", "Upload the flake report"):
        assert steps[name]["if"] == "${{ !cancelled() && needs.split.outputs.source == '' }}"
    assert steps["Upload durations"]["if"] == "${{ !cancelled() && hashFiles('durations.json') != '' }}"


def _summarized(tmp_path: Path, *, reused: bool, failing: int | None = None) -> tuple[int, list[str], Path, Path]:
    """Run the copy job's coverage step over twenty laid out legs with a python that records each call."""
    import yaml

    jobs = yaml.safe_load((Path(__file__).parents[1] / ".github/workflows/test.yml").read_text())["jobs"]
    step = next(
        step for step in jobs["unit"]["steps"] if step.get("name") == "Check and summarize every push leg's coverage"
    )
    assert step["if"] == "needs.split.outputs.source != ''"
    assert step["env"] == {"PUSH_LEGS": "${{ runner.temp }}/push-legs", "PYTHONPATH": "${{ github.workspace }}"}
    legs, workspace, tools = tmp_path / "push-legs", tmp_path / "workspace", tmp_path / "bin"
    for leg in range(1, 21):
        coverage = legs / f"coverage-3.12-{leg}"
        coverage.mkdir(parents=True)
        (coverage / ".coverage").touch()
        if reused:
            (coverage / "reused-coverage").mkdir()
            (coverage / "reused-coverage" / ".coverage").touch()
        (legs / f"executed-lines-3.12-{leg}").mkdir()
    workspace.mkdir()
    tools.mkdir()
    (tools / "python").write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$*" == "-m scripts.ci_cpu_quota" ]]; then echo 3; exit 0; fi\n'
        'echo "$PWD|$*" >> "$CALLS"\n'
        'if [[ "$1 $2" == "-m scripts.coverage_check" && "$PWD" == */coverage-3.12-"$FAILING" ]]; then exit 1; fi\n'
    )
    (tools / "python").chmod(0o755)
    calls = tmp_path / "calls"
    env = {
        **os.environ,
        "PATH": f"{tools}:{os.environ['PATH']}",
        "CALLS": str(calls),
        "FAILING": str(failing or ""),
        "PUSH_LEGS": str(legs),
        "GITHUB_WORKSPACE": str(workspace),
    }
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", step["run"]], env=env, capture_output=True, check=False
    )
    return result.returncode, calls.read_text().splitlines(), legs, workspace


@pytest.mark.feature("dev-push-copies-reused-unit-results-in-one-job")
@pytest.mark.parametrize("reused", [False, True], ids=["own", "reused"])
def test_the_copy_job_checks_and_summarizes_each_push_leg_as_its_unit_leg_does(tmp_path: Path, reused: bool) -> None:
    code, calls, legs, workspace = _summarized(tmp_path, reused=reused)
    assert code == 0
    databases = ".coverage reused-coverage/.coverage" if reused else ".coverage"
    for leg in range(1, 21):
        here, lines = legs / f"coverage-3.12-{leg}", legs / f"executed-lines-3.12-{leg}"
        check = f"{here}|-m scripts.coverage_check {databases}"
        summary = (
            f"{here}|-m tests.coverage_summary --out {lines}/executed-lines.json {databases}"
            f" --statements {lines}/statement-lines.json --slice {leg}/20 --head {workspace}"
        )
        assert calls.count(check) == calls.count(summary) == 1
        assert calls.index(check) < calls.index(summary)
    assert len(calls) == 40


@pytest.mark.feature("dev-push-copies-reused-unit-results-in-one-job")
def test_an_unusable_push_leg_coverage_database_fails_the_copy_job(tmp_path: Path) -> None:
    code, calls, legs, _ = _summarized(tmp_path, reused=False, failing=7)
    assert code != 0
    assert f"{legs}/coverage-3.12-7|-m scripts.coverage_check .coverage" in calls
    assert not [call for call in calls if call.startswith(f"{legs}/coverage-3.12-7|-m tests.coverage_summary")]


class _Context(dict):
    """A workflow expression context whose keys read as attributes."""

    def __getattr__(self, key: str) -> object:
        value = self[key]
        return _Context(value) if isinstance(value, dict) else value


def _evaluated(value: object, context: dict[str, object]) -> object:
    """Evaluate a workflow expression with the operators and functions the unit matrix uses."""
    if not isinstance(value, str) or not value.startswith("${{"):
        return value
    body = value.removeprefix("${{").removesuffix("}}").replace("&&", " and ").replace("||", " or ")
    names = {**context, "fromJSON": json.loads, "format": lambda template, *args: template.format(*args)}
    return eval(body.strip(), {"__builtins__": {}}, names)


def _legs(event: str, source: str, queue: str = "") -> list[dict[str, object]]:
    """Expand the unit matrix as GitHub does for one run: the product, then every exclude, then every include."""
    import yaml

    workflow = yaml.safe_load((Path(__file__).parents[1] / ".github/workflows/test.yml").read_text())
    matrix = workflow["jobs"]["unit"]["strategy"]["matrix"]
    context: dict[str, object] = {
        "github": _Context(event_name=event),
        "needs": _Context(split={"outputs": {"source": source, "queue_312": queue}}),
    }
    legs = [
        {"python-version": version, "shard": shard} for version in matrix["python-version"] for shard in matrix["shard"]
    ]
    for row in matrix["exclude"]:
        dropped = {key: _evaluated(value, context) for key, value in row.items()}
        legs = [leg for leg in legs if any(leg[key] != value for key, value in dropped.items())]
    for entry in cast(list[dict[str, object]], _evaluated(matrix["include"], context)):
        extended = [
            leg for leg in legs if all(leg[key] == entry[key] for key in ("python-version", "shard") if key in entry)
        ]
        for leg in extended:
            leg.update(entry)
        if not extended:
            legs.append(dict(entry))
    return legs


@pytest.mark.feature("dev-push-copies-reused-unit-results-in-one-job")
def test_a_reusing_dev_push_runs_one_copy_job_and_every_other_run_runs_every_leg() -> None:
    import yaml

    every = [{"python-version": "3.12", "shard": shard} for shard in range(1, 21)]
    assert _legs("push", "44301321341") == [{"python-version": "3.12", "shard": "copy"}]
    for event in ("push", "pull_request", "workflow_dispatch"):
        assert _legs(event, "") == every
    plan = ",".join(json.dumps({"python-version": "3.12", "shard": shard, "total": 14}) for shard in range(1, 15))
    assert [leg["shard"] for leg in _legs("merge_group", "", plan)] == list(range(1, 15))
    unit = yaml.safe_load((Path(__file__).parents[1] / ".github/workflows/test.yml").read_text())["jobs"]["unit"]
    named = unit["name"].replace("${{ matrix.python-version }}", "3.12").replace("${{ matrix.shard }}", "copy")
    assert named == ci_test_plan.COPY_JOB


@pytest.mark.feature("dev-push-copies-reused-unit-results-in-one-job")
@pytest.mark.parametrize(
    ("copied", "unit", "code", "said"),
    [
        ("44301321341", "success", 0, ""),
        (
            "44301321341",
            "failure",
            1,
            "::error::The unit job that copies queue run 44301321341 into every leg ended failure.\n",
        ),
        ("", "success", 0, ""),
    ],
    ids=["copied", "copy-failed", "legs"],
)
def test_shard_check_takes_the_one_copy_job_for_every_leg_of_a_reusing_push(
    tmp_path: Path, copied: str, unit: str, code: int, said: str
) -> None:
    import yaml

    jobs = yaml.safe_load((Path(__file__).parents[1] / ".github/workflows/test.yml").read_text())["jobs"]
    step = next(step for step in jobs["shard-check"]["steps"] if step.get("name") == "Wait for every planned shard")
    assert step["env"]["COPIED"] == "${{ needs.split.outputs.source }}"
    assert step["env"]["UNIT"] == "${{ needs.unit.result }}"
    tools = tmp_path / "bin"
    tools.mkdir()
    (tools / "python").write_text('#!/usr/bin/env bash\necho "$@" >> "$CALLS"\n')
    (tools / "python").chmod(0o755)
    calls, exported = tmp_path / "calls", tmp_path / "env"
    env = {
        **os.environ,
        "PATH": f"{tools}:{os.environ['PATH']}",
        "CALLS": str(calls),
        "GITHUB_ENV": str(exported),
        "QUEUE_PLAN": "",
        "COPIED": copied,
        "UNIT": unit,
    }
    result = subprocess.run(["bash", "-e", "-c", step["run"]], env=env, capture_output=True, text=True, check=False)
    assert result.returncode == code
    assert result.stdout == said
    assert exported.read_text() == "SHARDS=[" + ",".join(str(shard) for shard in range(1, 21)) + "]\n"
    assert (calls.read_text() if calls.exists() else "") == ("" if copied else "-m scripts.ci_gate_shards\n")


def test_artifact_contract_matches_the_eighteen_shards():
    assert ci_dev_reuse.artifact_names() == EXPECTED_ARTIFACTS


@pytest.mark.parametrize("missing", sorted(EXPECTED_ARTIFACTS))
def test_each_missing_artifact_falls_back(tmp_path, missing):
    _, _, folder = evidence(tmp_path)
    items = [json.loads(line) for line in (folder / "artifacts.jsonl").read_text().splitlines()]
    (folder / "artifacts.jsonl").write_text(
        "".join(json.dumps(item) + "\n" for item in items if item["name"] != missing)
    )
    assert ci_dev_reuse.select_source(tmp_path, HEAD, REPOSITORY) is None


@pytest.mark.parametrize("failed", ["unit (3.12, 1)", "unit (3.12, 12)"])
def test_each_edge_shard_must_pass(tmp_path, failed):
    _, _, folder = evidence(tmp_path)
    jobs = [json.loads(line) for line in (folder / "jobs.jsonl").read_text().splitlines()]
    for job in jobs:
        if job["name"] == failed:
            job["conclusion"] = "failure"
    (folder / "jobs.jsonl").write_text("".join(json.dumps(job) + "\n" for job in jobs))
    assert ci_dev_reuse.select_source(tmp_path, HEAD, REPOSITORY) is None


@pytest.mark.parametrize(
    "field,value", [("version", 2), ("run", 789), ("attempt", 1), ("event", "push"), ("commit", "d" * 40)]
)
def test_signed_mismatched_provenance_falls_back(tmp_path, field, value, capsys):
    _, record, folder = evidence(tmp_path)
    record[field] = value
    attest(folder, record)
    assert ci_dev_reuse.select_source(tmp_path, HEAD, REPOSITORY) is None
    if field != "commit":
        assert capsys.readouterr().out == "No dev unit reuse from run 123: provenance does not describe this attempt\n"


@pytest.mark.parametrize("mismatch", ["head", "commit", "source_id", "source_event", "source_tree", "source_inputs"])
def test_unusable_newest_candidate_does_not_hide_a_passed_candidate(tmp_path, mismatch):
    bad, record, folder = evidence(tmp_path, reused=mismatch.startswith("source"), run_id=789)
    good, _, _ = evidence(tmp_path)
    if mismatch == "head":
        bad["head_sha"] = "d" * 40
    elif mismatch == "commit":
        record["commit"] = "d" * 40
        attest(folder, record)
    else:
        source, source_record, source_folder = source_evidence(tmp_path, bad, record, folder)
        if mismatch == "source_id":
            attest(folder, record, source="999")
        elif mismatch == "source_event":
            source["event"] = "push"
            source_record["event"] = "push"
            attest(source_folder, source_record)
            (folder / "source.json").write_text(json.dumps(source))
        elif mismatch == "source_tree":
            source_record["tree"] = "d" * 40
            attest(source_folder, source_record)
        else:
            source_record["inputs"] = dict(record["inputs"], workflow="d" * 40)
            attest(source_folder, source_record)
    (tmp_path / "runs.json").write_text(json.dumps({"workflow_runs": [bad, good]}))
    assert ci_dev_reuse.select_source(tmp_path, HEAD, REPOSITORY) == 123


def test_source_may_not_reuse_a_third_run(tmp_path):
    queue, record, folder = evidence(tmp_path, reused=True)
    _, source_record, source_folder = source_evidence(tmp_path, queue, record, folder)
    source_record["reused"] = True
    attest(source_folder, source_record)
    assert ci_dev_reuse.select_source(tmp_path, HEAD, REPOSITORY) is None


def test_dependency_day_is_utc(tmp_path, monkeypatch):
    evidence(tmp_path)

    class Clock:
        @staticmethod
        def now(tz):
            assert tz is UTC
            return datetime.now(UTC)

    monkeypatch.setattr(ci_dev_reuse, "datetime", Clock)
    assert ci_dev_reuse.select_source(tmp_path, HEAD, REPOSITORY) == 123


def test_success_log_names_the_commit_and_source(tmp_path, capsys):
    evidence(tmp_path)
    assert ci_dev_reuse.select_source(tmp_path, HEAD, REPOSITORY) == 123
    assert capsys.readouterr().out == f"Reusing unit artifacts from passed queue evidence on {HEAD}: run 123\n"


def test_rejection_log_names_the_run_and_cause(tmp_path, capsys):
    _, _, folder = evidence(tmp_path)
    (folder / "reuse.log").write_text("")
    assert ci_dev_reuse.select_source(tmp_path, HEAD, REPOSITORY) is None
    assert capsys.readouterr().out == "No dev unit reuse from run 123: unauthenticated provenance\n"


@pytest.mark.parametrize("missing", ["--head", "--repository", "--evidence"])
def test_cli_requires_all_metadata_inputs(tmp_path, missing):
    values = {"--head": HEAD, "--repository": REPOSITORY, "--evidence": str(tmp_path)}
    argv = [value for key, value in values.items() if key != missing for value in (key, value)]
    with pytest.raises(SystemExit) as error:
        ci_dev_reuse.main(argv)
    assert error.value.code == 2


def test_cli_without_an_output_file_reports_the_source(tmp_path, monkeypatch, capsys):
    evidence(tmp_path)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    assert ci_dev_reuse.main(["--head", HEAD, "--repository", REPOSITORY, "--evidence", str(tmp_path)]) == 0
    assert capsys.readouterr().out == f"Reusing unit artifacts from passed queue evidence on {HEAD}: run 123\n"


def test_a_queue_run_that_reused_tests_never_feeds_a_dev_push(tmp_path):
    _, _, folder = evidence(tmp_path)
    with (folder / "artifacts.jsonl").open("a") as items:
        items.write(json.dumps({"name": "reused-tests-3.12", "expired": False}) + "\n")
    assert ci_dev_reuse.select_source(tmp_path, HEAD, REPOSITORY) is None


@pytest.mark.parametrize("expired", [False, True])
def test_a_run_that_reused_its_pull_requests_earlier_runs_never_feeds_a_dev_push(tmp_path, expired):
    _, _, folder = evidence(tmp_path)
    with (folder / "artifacts.jsonl").open("a") as items:
        items.write(json.dumps({"name": "reused-branch-3.12", "expired": expired}) + "\n")
    assert ci_dev_reuse.select_source(tmp_path, HEAD, REPOSITORY) is None


def test_a_queue_reused_pull_request_that_reused_its_earlier_runs_never_feeds_a_dev_push(tmp_path):
    queue, record, folder = evidence(tmp_path, reused=True)
    _, _, source_folder = source_evidence(tmp_path, queue, record, folder)
    with (source_folder / "artifacts.jsonl").open("a") as items:
        items.write(json.dumps({"name": "reused-branch-3.12", "expired": False}) + "\n")
    assert ci_dev_reuse.select_source(tmp_path, HEAD, REPOSITORY) is None
