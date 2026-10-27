import io
import json
import os
import subprocess
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from scripts import ci_stats_seed as seed
from scripts.ci_mutation.stats_dependencies import CONTEXT

pytestmark = pytest.mark.unit

KEEP, SCOPED, STALE = "a" * 64, "b" * 64, "c" * 64


def _archive(files):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as zipped:
        for name, text in files.items():
            zipped.writestr(name, text)
    return data.getvalue()


def _record(folder, pointer, key, scope, context=None):
    (folder / f"{pointer}.index").write_text(key)
    (folder / f"{key}.json").write_text(json.dumps({"key": key, "scope": scope, "context": context}))


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


def test_prune_keeps_scoped_and_unscoped_records_a_pointer_names(folder):
    _record(folder, "p1", KEEP, {})
    _record(folder, "p2", SCOPED, {"hooks/a.py": [3]})
    (folder / f"{STALE}.json").write_text(json.dumps({"key": STALE, "scope": {}}))
    (folder / "p3.index").write_text("d" * 64)
    (folder / "p4.index").write_text("not a key")

    assert seed.prune(folder) == 2

    assert sorted(path.name for path in folder.iterdir()) == [f"{KEEP}.json", f"{SCOPED}.json", "p1.index", "p2.index"]


def test_a_pull_request_record_survives_the_prune_and_serves_a_branch_that_leaves_its_inputs(folder):
    import hashlib

    from scripts.ci_mutation.stats_dependencies import StoredStats

    (folder / "module.py").write_text("value = 1\n")
    inputs = {"module.py": hashlib.sha256(b"value = 1\n").hexdigest()}
    context = {"inventory": ["module.py"]}
    pull = StoredStats(folder, folder, context, {"module.py": [1]})
    result = {"status": 0, "tests": {"module.x_value": ["t"]}, "durations": {"t": 1}, "cpu": 1}
    pull.save("tests/test_module.py", result, inputs, True)

    assert seed.prune(folder) == 1

    assert StoredStats(folder, folder, context, {}).load("tests/test_module.py") == {**result, "reused": True}
    other = StoredStats(folder, folder, context, {"module.py": [2]})
    assert other.load("tests/test_module.py") is None
    assert other.misses == {"scope": 1}


def test_prune_drops_records_made_under_another_lock_file_or_mutation_scripts(folder):
    _record(folder, "p1", KEEP, {}, "new")
    _record(folder, "p2", SCOPED, {}, "old")
    _record(folder, "p3", STALE, {})
    (folder / CONTEXT).write_text("new\n")

    assert seed.prune(folder) == 1

    assert sorted(path.name for path in folder.iterdir()) == [f"{KEEP}.json", CONTEXT, "p1.index"]


def test_stats_runs_mark_their_context_and_the_seed_prunes_by_it(folder):
    from scripts.ci_mutation.stats_dependencies import StoredStats

    old = StoredStats(folder, folder, {"sources": {"uv.lock": "1"}, "inventory": []}, {})
    new = StoredStats(folder, folder, {"sources": {"uv.lock": "2"}, "inventory": ["x"]}, {})
    old.save("tests/test_a.py", {"status": 0}, {}, False)
    new.save("tests/test_a.py", {"status": 0}, {}, False)
    new.save("tests/test_b.py", {"status": 0}, {}, False)
    (folder / CONTEXT).write_text(new.context_digest())

    assert seed.prune(folder) == 2

    assert old.load("tests/test_a.py") is None
    assert new.load("tests/test_a.py") == {"status": 0}


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


def test_merged_pulls_takes_only_the_head_when_the_start_is_unknown(tmp_path, monkeypatch, capfd):
    shas = _repo(tmp_path, ["Fix one (#10)", "Fix two (#11)"])
    monkeypatch.chdir(tmp_path)

    assert seed.merged_pulls(tmp_path, shas[0], "0" * 40) == [11]
    assert capfd.readouterr() == ("", "")


def test_merged_pulls_reads_only_subjects_and_at_most_the_depth(tmp_path, monkeypatch):
    shas = _repo(tmp_path, ["Fix one (#10)", "Fix two (#11)\n\nFollows (#9)"])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(seed, "DEPTH", 1)

    assert seed.merged_pulls(tmp_path, shas[0], shas[2]) == [11]


def test_git_fails_when_the_command_fails(tmp_path, monkeypatch):
    _repo(tmp_path, [])
    monkeypatch.chdir(tmp_path)

    with pytest.raises(subprocess.CalledProcessError):
        seed.git("rev-parse", "--verify", "no-such-ref")


def _fake_gh(tmp_path, monkeypatch, body):
    folder = tmp_path / "bin"
    folder.mkdir()
    (folder / "gh").write_text(f"#!/usr/bin/env bash\n{body}\n")
    (folder / "gh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{folder}:{os.environ['PATH']}")


def test_api_asks_gh_for_the_repository_path(tmp_path, monkeypatch):
    _fake_gh(tmp_path, monkeypatch, 'printf \'{"args": "%s"}\' "$*"')

    assert seed.api("pulls/7") == {"args": "api repos/{owner}/{repo}/pulls/7"}
    assert seed.api("pulls/7", binary=True) == b'{"args": "api repos/{owner}/{repo}/pulls/7"}'


def test_api_fails_when_gh_fails(tmp_path, monkeypatch):
    _fake_gh(tmp_path, monkeypatch, "echo '{}'; exit 1")

    with pytest.raises(subprocess.CalledProcessError):
        seed.api("pulls/7")


def test_harvest_copies_records_from_the_newest_run_with_stats_artifacts(folder, monkeypatch):
    archives = {
        5: _archive({"x.index": "old", "x.json": "{}"}),
        6: _archive({"x.index": "new", f"{KEEP}.json": "{}", "collected-0": "", CONTEXT: "c"}),
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
        seed,
        "api",
        lambda path, binary=False, deadline=None: archives[int(path.split("/")[2])] if binary else responses[path],
    )

    assert seed.harvest(7, folder, set()) == 5
    assert (folder / "x.index").read_text() == "new"
    assert sorted(path.name for path in folder.iterdir()) == [f"{KEEP}.json", CONTEXT, "x.index", "x.json"]


def test_harvest_skips_a_number_that_is_not_a_pull_request(tmp_path, monkeypatch, capsys):
    def missing(path, binary=False, deadline=None):
        raise subprocess.CalledProcessError(1, ["gh", "api", path], stderr=b"gh: Not Found (HTTP 404)\n")

    monkeypatch.setattr(seed, "api", missing)

    assert seed.harvest(7, tmp_path, set()) == 0
    assert capsys.readouterr().out == "Mutation stats seed: #7 is not a readable pull request\n"


def test_harvest_fails_when_github_cannot_answer(tmp_path, monkeypatch):
    def unavailable(path, binary=False, deadline=None):
        raise subprocess.CalledProcessError(1, ["gh", "api", path], stderr=b"gh: Service Unavailable (HTTP 503)\n")

    monkeypatch.setattr(seed, "api", unavailable)

    with pytest.raises(subprocess.CalledProcessError):
        seed.harvest(7, tmp_path, set())


def test_harvest_without_stats_artifacts_writes_nothing(tmp_path, monkeypatch):
    responses = {
        "pulls/7": {"head": {"sha": "abc"}},
        f"{seed.TESTS}?event=pull_request&head_sha=abc&per_page=10": {"workflow_runs": []},
    }
    monkeypatch.setattr(seed, "api", lambda path, binary=False, deadline=None: responses[path])

    assert seed.harvest(7, tmp_path, set()) == 0


def test_main_seeds_prunes_and_records_the_head(tmp_path, monkeypatch, capsys):
    folder = tmp_path / "a" / "cache"
    monkeypatch.setattr(
        seed, "merged_pulls", lambda found, head, before: [3] if (found, head, before) == (folder, "h", "b") else []
    )

    def harvest(number, found, taken, deadline):
        assert (number, found, taken) == (3, folder, set())
        _record(found, "p1", KEEP, {})
        _record(found, "p2", SCOPED, {"hooks/a.py": [1]})
        (found / f"{STALE}.json").write_text("{}")
        return 5

    monkeypatch.setattr(seed, "harvest", harvest)

    assert seed.main([str(folder), "--head", "h", "--before", "b"]) == 0

    assert (folder / seed.SEEDED).read_text() == "h\n"
    assert sorted(path.name for path in folder.iterdir()) == [
        f"{KEEP}.json",
        f"{SCOPED}.json",
        "p1.index",
        "p2.index",
        seed.SEEDED,
    ]
    after = sum(path.stat().st_size for path in folder.iterdir() if path.name != seed.SEEDED)
    assert capsys.readouterr().out == (
        f"Mutation stats seed: 5 files from pull requests [3], none skipped; kept 2 records; "
        f"{after + len('{}')} bytes before the prune, {after} after\n"
    )


def test_main_harvests_newest_first_and_skips_pulls_past_the_budget(folder, monkeypatch, capsys):
    monkeypatch.setattr(seed, "time", SimpleNamespace(monotonic=lambda: 7.0))
    monkeypatch.setattr(seed, "merged_pulls", lambda found, head, before: [1, 2, 3])
    harvested = []

    def harvest(number, found, taken, deadline):
        harvested.append(number)
        assert deadline == 7.0 + seed.BUDGET
        if number == 1:
            raise subprocess.TimeoutExpired(["gh"], 0)
        return number - 1

    monkeypatch.setattr(seed, "harvest", harvest)

    assert seed.main([str(folder), "--head", "h", "--before", "b"]) == 0

    assert harvested == [3, 2, 1]
    assert (
        "3 files from pull requests [1, 2, 3], skipped [1] on a GitHub timeout or past the budget, never retried;"
        in (capsys.readouterr().out)
    )
    assert (folder / seed.SEEDED).read_text() == "h\n"


def test_main_skips_a_pull_whose_github_call_timed_out(folder, monkeypatch, capsys):
    monkeypatch.setattr(seed, "merged_pulls", lambda found, head, before: [1, 2])

    def harvest(number, found, taken, deadline):
        if number == 2:
            raise subprocess.TimeoutExpired(["gh"], seed.TIMEOUT)
        return 1

    monkeypatch.setattr(seed, "harvest", harvest)

    assert seed.main([str(folder), "--head", "h", "--before", "b"]) == 0

    assert (
        "1 files from pull requests [1, 2], skipped [2] on a GitHub timeout or past the budget, never retried;"
        in capsys.readouterr().out
    )


def test_harvest_keeps_pointers_a_newer_pull_request_already_wrote(folder, monkeypatch):
    responses = {
        "pulls/7": {"head": {"sha": "abc"}},
        f"{seed.TESTS}?event=pull_request&head_sha=abc&per_page=10": {"workflow_runs": [{"id": 1}]},
        "actions/runs/1/artifacts?per_page=100": {
            "artifacts": [{"id": 5, "name": "mutation-test-stats-cache-0", "expired": False}]
        },
    }
    archive = _archive({"x.index": "old", "y.index": "old", f"{KEEP}.json": "{}"})
    monkeypatch.setattr(seed, "api", lambda path, binary=False, deadline=None: archive if binary else responses[path])
    (folder / "x.index").write_text("new")
    taken = {"x.index"}

    assert seed.harvest(7, folder, taken) == 2

    assert (folder / "x.index").read_text() == "new"
    assert (folder / "y.index").read_text() == "old"
    assert taken == {"x.index", "y.index"}


def test_harvest_passes_the_deadline_to_every_github_call(folder, monkeypatch):
    responses = {
        "pulls/7": {"head": {"sha": "abc"}},
        f"{seed.TESTS}?event=pull_request&head_sha=abc&per_page=10": {"workflow_runs": [{"id": 1}]},
        "actions/runs/1/artifacts?per_page=100": {
            "artifacts": [{"id": 5, "name": "mutation-test-stats-cache-0", "expired": False}]
        },
    }
    deadlines = []

    def api(path, binary=False, deadline=None):
        deadlines.append(deadline)
        return _archive({"x.index": "k"}) if binary else responses[path]

    monkeypatch.setattr(seed, "api", api)

    assert seed.harvest(7, folder, set(), 42.0) == 1
    assert deadlines == [42.0] * 4


def test_harvest_keeps_pointers_it_wrote_before_a_timeout(folder, monkeypatch):
    responses = {
        "pulls/7": {"head": {"sha": "abc"}},
        f"{seed.TESTS}?event=pull_request&head_sha=abc&per_page=10": {"workflow_runs": [{"id": 1}]},
        "actions/runs/1/artifacts?per_page=100": {
            "artifacts": [
                {"id": 5, "name": "mutation-test-stats-cache-0", "expired": False},
                {"id": 6, "name": "mutation-test-stats-cache-1", "expired": False},
            ]
        },
    }

    def api(path, binary=False, deadline=None):
        if path == "actions/artifacts/6/zip":
            raise subprocess.TimeoutExpired(["gh"], 0)
        return _archive({"x.index": "k"}) if binary else responses[path]

    monkeypatch.setattr(seed, "api", api)
    taken = set()

    with pytest.raises(subprocess.TimeoutExpired):
        seed.harvest(7, folder, taken)

    assert taken == {"x.index"}


@pytest.mark.parametrize(("deadline", "timeout"), [(150.0, 50.0), (100.5, 0.5)])
def test_api_waits_at_most_until_the_deadline(monkeypatch, deadline, timeout):
    seen = {}

    def run(command, **options):
        seen.update(options)
        return subprocess.CompletedProcess(command, 0, b"{}")

    monkeypatch.setattr(seed.subprocess, "run", run)
    monkeypatch.setattr(seed, "time", SimpleNamespace(monotonic=lambda: 100.0))

    assert seed.api("pulls/7", deadline=deadline) == {}
    assert seen["timeout"] == timeout


@pytest.mark.parametrize("deadline", [100.0, 90.0])
def test_api_refuses_a_call_at_or_past_the_deadline(monkeypatch, deadline):
    monkeypatch.setattr(seed.subprocess, "run", lambda *args, **options: pytest.fail("gh ran"))
    monkeypatch.setattr(seed, "time", SimpleNamespace(monotonic=lambda: 100.0))

    with pytest.raises(subprocess.TimeoutExpired) as error:
        seed.api("pulls/7", deadline=deadline)

    assert (error.value.cmd, error.value.timeout) == (["gh", "api", "pulls/7"], 0)


def test_api_gives_each_gh_call_a_timeout(monkeypatch):
    seen = {}

    def run(command, **options):
        seen.update(options)
        return subprocess.CompletedProcess(command, 0, b"{}")

    monkeypatch.setattr(seed.subprocess, "run", run)

    assert seed.api("pulls/7") == {}
    assert seen["timeout"] == seed.TIMEOUT < seed.BUDGET


def test_main_reuses_an_existing_folder(folder, monkeypatch):
    monkeypatch.setattr(seed, "merged_pulls", lambda found, head, before: [])

    assert seed.main([str(folder), "--head", "h", "--before", "b"]) == 0
    assert (folder / seed.SEEDED).read_text() == "h\n"


@pytest.mark.parametrize("given", [["--head", "h"], ["--before", "b"]])
def test_main_requires_the_head_and_the_push_before(folder, given):
    with pytest.raises(SystemExit):
        seed.main([str(folder), *given])


def test_dev_pushes_save_the_stats_cache_pull_request_runs_restore():
    jobs = yaml.safe_load((Path(__file__).parents[1] / ".github/workflows/test.yml").read_text())["jobs"]
    key = "mutation-test-stats-v2-${{ runner.os }}-${{ github.run_id }}-${{ github.run_attempt }}"
    restored = next(s for s in jobs["mutation-stats"]["steps"] if s.get("name") == "Restore compatible test stats")
    job = jobs["mutation-stats-seed"]
    steps = {step.get("name"): step for step in job["steps"]}
    assert seed.BUDGET * 2 <= job["timeout-minutes"] * 60
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
