import io
import json
import subprocess
import zipfile
from pathlib import Path

import pytest
import yaml

from scripts import ci_stats_seed as seed

pytestmark = pytest.mark.unit

KEEP, SCOPED, STALE = "a" * 64, "b" * 64, "c" * 64


def _archive(files):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as zipped:
        for name, text in files.items():
            zipped.writestr(name, text)
    return data.getvalue()


def _record(folder, pointer, key, scope):
    (folder / f"{pointer}.index").write_text(key)
    (folder / f"{key}.json").write_text(json.dumps({"key": key, "scope": scope}))


def _repo(tmp_path, subjects):
    run = lambda *args: subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True)
    run("init", "-q")
    run("-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "--allow-empty", "-m", "start")
    for subject in subjects:
        run("-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "--allow-empty", "-m", subject)
    return [line for line in run("log", "--format=%H").stdout.split()]


def test_prune_keeps_only_unscoped_records_a_pointer_names(tmp_path):
    _record(tmp_path, "p1", KEEP, {})
    _record(tmp_path, "p2", SCOPED, {"hooks/a.py": [3]})
    (tmp_path / f"{STALE}.json").write_text(json.dumps({"key": STALE, "scope": {}}))
    (tmp_path / "p3.index").write_text("d" * 64)
    (tmp_path / "p4.index").write_text("not a key")

    assert seed.prune(tmp_path) == 1

    assert sorted(path.name for path in tmp_path.iterdir()) == [f"{KEEP}.json", "p1.index"]


def test_prune_drops_a_pointer_to_an_unreadable_record(tmp_path):
    (tmp_path / "p1.index").write_text(KEEP)
    (tmp_path / f"{KEEP}.json").write_text("{")

    assert seed.prune(tmp_path) == 0
    assert list(tmp_path.iterdir()) == []


def test_merged_pulls_reads_squash_subjects_since_the_seeded_head(tmp_path, monkeypatch):
    shas = _repo(tmp_path, ["Fix one (#10)", "Fix two (#11)", "No number", "Fix three (#12)"])
    monkeypatch.chdir(tmp_path)
    folder = tmp_path / "cache"
    folder.mkdir()
    (folder / seed.SEEDED).write_text(f"{shas[3]}\n")

    assert seed.merged_pulls(folder, shas[0], shas[1]) == [11, 12]


def test_merged_pulls_falls_back_to_the_push_before_without_a_seeded_head(tmp_path, monkeypatch):
    shas = _repo(tmp_path, ["Fix one (#10)", "Fix two (#11)"])
    monkeypatch.chdir(tmp_path)

    assert seed.merged_pulls(tmp_path, shas[0], shas[2]) == [10, 11]


def test_merged_pulls_takes_only_the_head_when_the_start_is_unknown(tmp_path, monkeypatch):
    shas = _repo(tmp_path, ["Fix one (#10)", "Fix two (#11)"])
    monkeypatch.chdir(tmp_path)

    assert seed.merged_pulls(tmp_path, shas[0], "0" * 40) == [11]


def test_harvest_copies_records_from_the_newest_run_with_stats_artifacts(tmp_path, monkeypatch):
    archives = {
        5: _archive({"x.index": "old", "x.json": "{}"}),
        6: _archive({"x.index": "new", f"{KEEP}.json": "{}", "collected-0": ""}),
    }
    responses = {
        "pulls/7": {"head": {"sha": "abc"}},
        f"{seed.TESTS}?event=pull_request&head_sha=abc&per_page=10": {"workflow_runs": [{"id": 2}, {"id": 1}]},
        "actions/runs/2/artifacts?per_page=100": {"artifacts": [{"id": 9, "name": "mutation-report-0", "expired": False}]},
        "actions/runs/1/artifacts?per_page=100": {
            "artifacts": [
                {"id": 6, "name": "mutation-test-stats-cache-1", "expired": False},
                {"id": 5, "name": "mutation-test-stats-cache-0", "expired": False},
                {"id": 4, "name": "mutation-test-stats-cache-2", "expired": True},
            ]
        },
    }
    monkeypatch.setattr(
        seed, "api", lambda path, binary=False: archives[int(path.split("/")[2])] if binary else responses[path]
    )

    assert seed.harvest(7, tmp_path) == 4
    assert (tmp_path / "x.index").read_text() == "new"
    assert sorted(path.name for path in tmp_path.iterdir()) == [f"{KEEP}.json", "x.index", "x.json"]


def test_harvest_without_stats_artifacts_writes_nothing(tmp_path, monkeypatch):
    responses = {
        "pulls/7": {"head": {"sha": "abc"}},
        f"{seed.TESTS}?event=pull_request&head_sha=abc&per_page=10": {"workflow_runs": []},
    }
    monkeypatch.setattr(seed, "api", lambda path, binary=False: responses[path])

    assert seed.harvest(7, tmp_path) == 0


def test_main_seeds_prunes_and_records_the_head(tmp_path, monkeypatch, capsys):
    folder = tmp_path / "cache"
    monkeypatch.setattr(seed, "merged_pulls", lambda found, head, before: [3])

    def harvest(number, found):
        _record(found, "p1", KEEP, {})
        _record(found, "p2", SCOPED, {"hooks/a.py": [1]})
        return 4

    monkeypatch.setattr(seed, "harvest", harvest)

    assert seed.main([str(folder), "--head", "h", "--before", "b"]) == 0

    assert (folder / seed.SEEDED).read_text() == "h\n"
    assert sorted(path.name for path in folder.iterdir()) == [f"{KEEP}.json", "p1.index", seed.SEEDED]
    assert capsys.readouterr().out == "Mutation stats seed: 4 files from pull requests [3]; kept 1 records\n"


def test_dev_pushes_save_the_stats_cache_pull_request_runs_restore():
    jobs = yaml.safe_load((Path(__file__).parents[1] / ".github/workflows/test.yml").read_text())["jobs"]
    key = "mutation-test-stats-v2-${{ runner.os }}-${{ github.run_id }}-${{ github.run_attempt }}"
    restored = next(s for s in jobs["mutation-stats"]["steps"] if s.get("name") == "Restore compatible test stats")
    job = jobs["mutation-stats-seed"]
    steps = {step.get("name"): step for step in job["steps"]}
    assert job["if"] == "${{ !cancelled() && github.event_name == 'push' }}"
    assert job["permissions"] == {"contents": "read", "actions": "read", "pull-requests": "read"}
    assert steps["Restore the dev test stats"]["with"] == restored["with"]
    assert steps["Save the dev test stats"]["uses"] == "actions/cache/save@v4"
    assert steps["Save the dev test stats"]["with"] == {"path": ".mutation-stats-cache", "key": key}
    assert restored["with"]["restore-keys"] == "mutation-test-stats-v2-${{ runner.os }}-"
    assert "scripts.ci_stats_seed .mutation-stats-cache" in steps["Add the merged pull requests' test stats"]["run"]
    names = list(steps)
    assert names.index("Restore the dev test stats") < names.index("Add the merged pull requests' test stats")
    assert names.index("Add the merged pull requests' test stats") < names.index("Save the dev test stats")
    uploaded = next(
        s for s in jobs["mutation-stats"]["steps"] if s.get("name") == "Upload this part's test stats cache"
    )
    assert uploaded["with"]["name"].startswith(seed.ARTIFACT)
    assert "job-total" not in uploaded["if"]
