import hashlib
import json
import zipfile
from datetime import UTC, datetime, timedelta

import pytest

from scripts import ci_dev_reuse

HEAD = "a" * 40
TREE = "b" * 40
REPOSITORY = "the-cloud-clockwork/agentihooks"


def evidence(tmp_path, *, reused=False):
    run = {
        "id": 123,
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
        "run": 123,
        "attempt": 2,
        "event": "merge_group",
        "commit": HEAD,
        "tree": TREE,
        "reused": reused,
        "inputs": {"day": datetime.now(UTC).date().isoformat(), "workflow": "c" * 40},
    }
    folder = tmp_path / "123"
    folder.mkdir()
    with zipfile.ZipFile(folder / "proof.zip", "w") as archive:
        archive.writestr("provenance.json", json.dumps(record))
    digest = hashlib.sha256(json.dumps(record, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    (folder / "reuse.log").write_text(
        f"2026-10-13T00:00:00Z required-tree-sha256={digest}\n2026-10-13T00:00:00Z run=\n"
    )
    jobs = [{"name": "reuse", "conclusion": "success"}]
    for version, count in [("3.11", 6), ("3.12", 12)]:
        jobs += [{"name": f"unit ({version}, {shard})", "conclusion": "success"} for shard in range(1, count + 1)]
    (folder / "jobs.jsonl").write_text("".join(json.dumps(job) + "\n" for job in jobs))
    artifacts = [{"name": name, "expired": False} for name in ci_dev_reuse.artifact_names()]
    (folder / "artifacts.jsonl").write_text("".join(json.dumps(item) + "\n" for item in artifacts))
    (tmp_path / "runs.json").write_text(json.dumps({"workflow_runs": [run]}))
    return run, record, folder


def test_exact_passed_queue_commit_reuses_all_unit_artifacts(tmp_path):
    evidence(tmp_path)
    assert ci_dev_reuse.select_source(tmp_path, HEAD, REPOSITORY) == 123


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
def test_incomplete_evidence_falls_back(tmp_path, kind):
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
        with zipfile.ZipFile(folder / "proof.zip", "w") as archive:
            archive.writestr("provenance.json", json.dumps(record))
    assert ci_dev_reuse.select_source(tmp_path, HEAD, REPOSITORY) is None


def test_missing_metadata_falls_back(tmp_path):
    assert ci_dev_reuse.select_source(tmp_path, HEAD, REPOSITORY) is None
