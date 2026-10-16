import io
import json
import zipfile

import pytest

from scripts import ci_test_plan

REPOSITORY = "the-cloud-clockwork/agentihooks"
HEAD = "a" * 40

pytestmark = pytest.mark.unit


def _run(run_id, **changes):
    return {
        "id": run_id,
        "run_attempt": 1,
        "event": "push",
        "head_branch": "dev",
        "head_sha": f"{run_id:040d}",
        "status": "completed",
        "path": ".github/workflows/test.yml",
        "repository": {"full_name": REPOSITORY},
        **changes,
    }


def _zip(manifest):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
    return buffer.getvalue()


def _github(monkeypatch, runs, *, manifests, failed=()):
    def api(endpoint):
        if "/workflows/test.yml/runs?" in endpoint:
            return json.dumps({"workflow_runs": runs}).encode()
        if endpoint.endswith("/artifacts?per_page=100"):
            run_id = int(endpoint.split("/runs/")[1].split("/")[0])
            kept = [{"id": run_id, "name": "dep-manifest", "expired": False}] if run_id in manifests else []
            return json.dumps({"artifacts": [{"id": 0, "name": "durations-merged", "expired": False}, *kept]}).encode()
        return _zip(manifests[int(endpoint.split("/artifacts/")[1].split("/")[0])])

    def jobs(endpoint):
        run_id = int(endpoint.split("/runs/")[1].split("/")[0])
        return [
            {"name": name, "conclusion": "failure" if (run_id, name) in failed else "success"}
            for name in sorted(ci_test_plan.UNIT_JOBS)
        ]

    monkeypatch.setattr(ci_test_plan, "_api", api)
    monkeypatch.setattr(ci_test_plan, "read_jobs", jobs)


def _manifest(run_id):
    return {
        "commit": f"{run_id:040d}",
        "units": [],
        "tests": {"tests/test_a.py::test_a": {"units": [], "files": [], "escapes": False}},
    }


def test_the_newest_dev_run_whose_unit_shards_passed_and_kept_a_manifest_is_the_record(monkeypatch):
    runs = [_run(3), _run(2), _run(1)]
    _github(monkeypatch, runs, manifests={2: _manifest(2), 1: _manifest(1)}, failed={(2, "unit (3.11, 4)")})
    run, manifest = ci_test_plan.find_record(REPOSITORY)
    assert run["id"] == 1
    assert manifest["commit"] == f"{1:040d}"


@pytest.mark.parametrize(
    "field,value",
    [("event", "merge_group"), ("head_branch", "feature"), ("path", ".github/workflows/proofs.yml")],
)
def test_runs_that_are_not_dev_pushes_of_the_tests_workflow_are_no_record(monkeypatch, field, value):
    _github(monkeypatch, [_run(1, **{field: value})], manifests={1: _manifest(1)})
    assert ci_test_plan.find_record(REPOSITORY) is None


def test_a_manifest_for_another_commit_is_no_record(monkeypatch, capsys):
    _github(monkeypatch, [_run(1)], manifests={1: _manifest(9)})
    assert ci_test_plan.find_record(REPOSITORY) is None
    assert "recorded a manifest for" in capsys.readouterr().out


def test_a_missing_record_plans_a_full_run(monkeypatch, tmp_path, capsys):
    _github(monkeypatch, [_run(1)], manifests={})
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    out = tmp_path / "reused-tests.json"
    assert ci_test_plan.main(["--repository", REPOSITORY, "--head", HEAD, "--out", str(out)]) == 0
    assert not out.exists()
    assert output.read_text() == "reused=0\n"
    assert "every test runs" in capsys.readouterr().out


def test_a_record_plans_the_unchanged_tests(monkeypatch, tmp_path):
    _github(monkeypatch, [_run(1)], manifests={1: _manifest(1)})
    monkeypatch.setattr(ci_test_plan.dep_manifest, "changes", lambda repo, base, head: [])
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    out = tmp_path / "reused-tests.json"
    assert ci_test_plan.main(["--repository", REPOSITORY, "--head", HEAD, "--out", str(out)]) == 0
    assert json.loads(out.read_text())["tests"] == ["tests/test_a.py::test_a"]
    assert json.loads(out.read_text())["run"] == "1"
    assert output.read_text() == "reused=1\n"
