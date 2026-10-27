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
    def run(*args):
        return subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True)

    run("init", "-q")
    run("-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "--allow-empty", "-m", "start")
    for subject in subjects:
        run("-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "--allow-empty", "-m", subject)
    return run("log", "--format=%H").stdout.split()


@pytest.fixture
def folder(tmp_path):
    path = tmp_path / "cache"
    path.mkdir()
    return path


def test_prune_keeps_only_unscoped_records_a_pointer_names(folder):
    _record(folder, "p1", KEEP, {})
    _record(folder, "p2", SCOPED, {"hooks/a.py": [3]})
    (folder / f"{STALE}.json").write_text(json.dumps({"key": STALE, "scope": {}}))
    (folder / "p3.index").write_text("d" * 64)
    (folder / "p4.index").write_text("not a key")

    assert seed.prune(folder) == 1

    assert sorted(path.name for path in folder.iterdir()) == [f"{KEEP}.json", "p1.index"]


def test_prune_drops_a_pointer_to_an_unreadable_record(folder):
    (folder / "p1.index").write_text(KEEP)
    (folder / f"{KEEP}.json").write_text("{")

    assert seed.prune(folder) == 0
    assert list(folder.iterdir()) == []


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


def test_harvest_copies_records_from_the_newest_run_with_stats_artifacts(folder, monkeypatch):
    archives = {
        5: _archive({"x.index": "old", "x.json": "{}"}),
        6: _archive({"x.index": "new", f"{KEEP}.json": "{}", "collected-0": ""}),
    }
    responses = {
        "pulls/7": {"head": {"sha": "abc"}},
        f"{seed.TESTS}?event=pull_request&head_sha=abc&per_page=10": {"workflow_runs": [{"id": 2}, {"id": 1}]},
        "actions/runs/2/artifacts?per_page=100": {
            "artifacts": [{"id": 9, "name": "mutation-report-0", "expired": False}]
        },
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

    assert seed.harvest(7, folder) == 4
    assert (folder / "x.index").read_text() == "new"
    assert sorted(path.name for path in folder.iterdir()) == [f"{KEEP}.json", "x.index", "x.json"]


def test_harvest_skips_a_number_that_is_not_a_pull_request(tmp_path, monkeypatch, capsys):
    def missing(path, binary=False):
        raise subprocess.CalledProcessError(1, ["gh", "api", path], stderr=b"gh: Not Found (HTTP 404)\n")

    monkeypatch.setattr(seed, "api", missing)

    assert seed.harvest(7, tmp_path) == 0
    assert capsys.readouterr().out == "Mutation stats seed: #7 is not a readable pull request\n"


def test_harvest_fails_when_github_cannot_answer(tmp_path, monkeypatch):
    def unavailable(path, binary=False):
        raise subprocess.CalledProcessError(1, ["gh", "api", path], stderr=b"gh: Service Unavailable (HTTP 503)\n")

    monkeypatch.setattr(seed, "api", unavailable)

    with pytest.raises(subprocess.CalledProcessError):
        seed.harvest(7, tmp_path)


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
    assert job["permissions"] == {"contents": "read"}
    mint = next(step for step in job["steps"] if step.get("id") == "app-token")
    assert mint["with"]["permission-actions"] == mint["with"]["permission-pull-requests"] == "read"
    seeding = steps["Add the merged pull requests' test stats"]
    assert seeding["env"]["GH_TOKEN"] == "${{ steps.app-token.outputs.token }}"
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
