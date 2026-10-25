import io
import json
import subprocess
import zipfile

import pytest


@pytest.mark.parametrize("source", ["merged", "open", "foreign_base", "failed", "wrong_head", "expired"])
def test_statistics_seed_requires_a_successful_run_merged_into_the_current_base(tmp_path, monkeypatch, source):
    from scripts.ci_mutation import stats_seed

    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "--allow-empty", "-qm", "base"],
        cwd=root,
        check=True,
    )
    base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    head = "a" * 40
    key = "b" * 64
    pull = {
        "number": 1,
        "merged_at": None if source == "open" else "2026-10-25T00:00:00Z",
        "merge_commit_sha": "c" * 40 if source == "foreign_base" else base,
        "head": {"sha": head},
    }
    run = {
        "id": 42,
        "head_sha": "d" * 40 if source == "wrong_head" else head,
        "conclusion": "failure" if source == "failed" else "success",
    }
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr(f"{key}.json", json.dumps({"key": key, "status": 0}))
        zipped.writestr("../escaped.json", "{}")
    calls = []

    def api(endpoint):
        calls.append(endpoint)
        if endpoint.startswith("pulls?"):
            return [pull]
        if "/runs?" in endpoint:
            return {"workflow_runs": [run]}
        if endpoint == "actions/runs/42/artifacts?per_page=100":
            return {"artifacts": [{"id": 7, "name": "mutation-test-stats-0", "expired": source == "expired"}]}
        assert endpoint == "actions/artifacts/7/zip"
        return archive.getvalue()

    monkeypatch.setattr(stats_seed, "api", api)
    folder = tmp_path / "cache"
    count = stats_seed.seed(root, folder, base)
    assert count == (1 if source == "merged" else 0)
    assert sorted(path.name for path in folder.glob("*.json")) == ([f"{key}.json"] if source == "merged" else [])
    assert not (tmp_path / "escaped.json").exists()
    if source != "merged":
        assert "actions/artifacts/7/zip" not in calls
