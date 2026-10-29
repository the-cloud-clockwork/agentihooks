import hashlib
import json
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts import ci_dev_reuse

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


def evidence(tmp_path, *, reused=False, run_id=123):
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
    jobs += [{"name": f"unit (3.12, {shard})", "conclusion": "success"} for shard in range(1, 21)]
    (folder / "jobs.jsonl").write_text("".join(json.dumps(job) + "\n" for job in jobs))
    artifacts = [{"name": name, "expired": False} for name in sorted(EXPECTED_ARTIFACTS)]
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


def test_cli_publishes_empty_source_on_mismatch(tmp_path, monkeypatch):
    evidence(tmp_path)
    output = tmp_path / "outputs"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert ci_dev_reuse.main(["--head", "d" * 40, "--repository", REPOSITORY, "--evidence", str(tmp_path)]) == 0
    assert output.read_text() == "source=\n"


def test_cli_publishes_passed_source(tmp_path, monkeypatch):
    evidence(tmp_path)
    output = tmp_path / "outputs"
    output.write_text("existing=kept\n")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert ci_dev_reuse.main(["--head", HEAD, "--repository", REPOSITORY, "--evidence", str(tmp_path)]) == 0
    assert output.read_text() == "existing=kept\nsource=123\n"


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


def test_workflow_preserves_downstream_artifacts():
    import yaml

    jobs = yaml.safe_load((Path(__file__).parents[1] / ".github/workflows/test.yml").read_text())["jobs"]
    unit = jobs["unit"]
    steps = {step.get("name"): step for step in unit["steps"]}
    assert steps["Run tests"]["if"] == "needs.split.outputs.source == ''"
    for kind in ("durations", "flakes", "coverage"):
        restored = steps[f"Restore queue shard {kind}"]
        assert restored["with"]["run-id"] == "${{ needs.split.outputs.source }}"
    producer = "durations" if "durations" in jobs else "select"
    assert jobs["split"]["outputs"]["source"] == "${{ needs." + producer + ".outputs.source }}"
    assert jobs.get("durations", jobs["select"])["outputs"]["source"] == "${{ steps.unit-source.outputs.source }}"
    for job in ("sonar", "coverage-baseline", "refresh-durations", "flake-budget"):
        assert "unit" in jobs[job]["needs"]


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
