import subprocess
from pathlib import Path

import pytest

from scripts.swarm import ledger_host


def write_stat(proc, pid, ticks, start, system=0):
    fields = ["S", "1"] + ["0"] * 9 + [str(ticks - system), str(system), "777"] + ["0"] * 5 + [str(start)]
    (proc / str(pid)).mkdir(exist_ok=True)
    (proc / str(pid) / "stat").write_text(f"{pid} (python3 (ledger) server) " + " ".join(fields))


def test_server_cpu_is_sampled_and_its_age_read_from_proc(tmp_path):
    write_stat(tmp_path, 42, 1_000, 30_000, system=300)
    (tmp_path / "uptime").write_text("1200.00 900.00\n")

    def sleep(seconds):
        assert seconds == ledger_host.SAMPLE_S
        write_stat(tmp_path, 42, 1_091, 30_000, system=340)

    assert ledger_host.server(42, tmp_path, sleep, 100) == {"cpu": 182, "started_minutes": 15}


def test_server_cpu_rounds_and_its_age_floors_to_whole_minutes(tmp_path):
    write_stat(tmp_path, 42, 1_000, 18_000)
    (tmp_path / "uptime").write_text("1259.99 900.00\n")
    assert ledger_host.server(42, tmp_path, lambda s: write_stat(tmp_path, 42, 1_092, 18_000), 60) == {
        "cpu": 307,
        "started_minutes": 15,
    }


def test_an_unknown_or_vanished_server_reports_unknown(tmp_path):
    unknown = {"cpu": None, "started_minutes": None}
    assert ledger_host.server(None, tmp_path, lambda s: None, 100) == unknown
    assert ledger_host.server(7, tmp_path, lambda s: None, 100) == unknown


def test_the_server_pid_comes_from_the_ledger_folder(tmp_path):
    assert ledger_host.server_pid(tmp_path) is None
    (tmp_path / ".server.pid").write_text("4242")
    assert ledger_host.server_pid(tmp_path) == 4242
    (tmp_path / ".server.pid").write_text("nope")
    assert ledger_host.server_pid(tmp_path) is None
    assert ledger_host.folder({"LEDGER_DIR": str(tmp_path)}) == tmp_path
    assert ledger_host.folder({}).name == "development-ledger"


def test_pass_snapshot_reads_the_named_process_without_sleeping(tmp_path, monkeypatch):
    monkeypatch.setenv("LEDGER_DIR", str(tmp_path))
    monkeypatch.setattr(ledger_host, "PROC", tmp_path)
    monkeypatch.setattr(ledger_host.os, "sysconf", lambda name: 100 if name == "SC_CLK_TCK" else 0)
    assert ledger_host.snapshot() is None
    (tmp_path / ".server.pid").write_text("4242")
    assert ledger_host.snapshot() is None
    write_stat(tmp_path, 4242, 123, 45, system=23)
    assert ledger_host.snapshot() == {"pid": 4242, "start": 45, "ticks": 123, "hertz": 100}
    (tmp_path / "4242" / "stat").write_text("invalid")
    assert ledger_host.snapshot() is None


def test_cpu_over_a_pass_requires_the_same_process_and_measured_elapsed_time():
    before = {"pid": 1, "start": 20, "ticks": 100, "hertz": 100}
    after = {**before, "ticks": 300}
    assert ledger_host.cpu_over_pass(before, after, 8) == 25.0
    assert ledger_host.cpu_over_pass(before, {**after, "ticks": 125}, 0.5) == 50.0
    assert ledger_host.cpu_over_pass(before, after, 1.0) == 200.0
    assert ledger_host.cpu_over_pass(before, before, 8) == 0.0
    assert ledger_host.cpu_over_pass(None, after, 8) is None
    assert ledger_host.cpu_over_pass(before, None, 8) is None
    assert ledger_host.cpu_over_pass(before, after, 0) is None
    assert ledger_host.cpu_over_pass(before, after, -1) is None
    assert ledger_host.cpu_over_pass(after, before, 8) is None
    for field in ("pid", "start", "hertz"):
        assert ledger_host.cpu_over_pass(before, {**after, field: after[field] + 1}, 8) is None


def test_the_newest_commit_on_dev_counts_from_when_it_reached_dev_not_its_commit_date(monkeypatch, tmp_path):
    monkeypatch.setattr(ledger_host, "merged_at", lambda repo, sha: None)

    def git(*args, **env):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True, env=env or None)

    identity = {
        "GIT_AUTHOR_NAME": "a",
        "GIT_AUTHOR_EMAIL": "a@b",
        "GIT_COMMITTER_NAME": "a",
        "GIT_COMMITTER_EMAIL": "a@b",
        "PATH": "/usr/bin:/bin",
    }
    built = {**identity, "GIT_AUTHOR_DATE": "@1000000000 +0000", "GIT_COMMITTER_DATE": "@1000000000 +0000"}
    git("init", "-q")
    git("commit", "-q", "--allow-empty", "-m", "Time the ledger every pass", **built)
    now = 1_000_000_000 + 90 * 60
    assert ledger_host.newest_on_dev(tmp_path, now) == {"newest": None, "merged_minutes": None}
    git("-c", "core.logAllRefUpdates=false", "update-ref", "refs/remotes/origin/dev", "HEAD", **identity)
    assert ledger_host.newest_on_dev(tmp_path, now) == {"newest": None, "merged_minutes": None}
    git("update-ref", "-d", "refs/remotes/origin/dev", **identity)
    reached = {**identity, "GIT_COMMITTER_DATE": f"@{now - 5 * 60} +0000"}
    git("update-ref", "-m", "fetch origin dev: fast-forward", "refs/remotes/origin/dev", "HEAD", **reached)
    assert ledger_host.newest_on_dev(tmp_path, now) == {
        "newest": "Time the ledger every pass",
        "merged_minutes": 5,
    }


def test_a_merge_queue_commit_is_aged_from_its_pull_request_merge_not_its_older_commit_time(monkeypatch, tmp_path):
    real = subprocess.run

    def run(argv, **kwargs):
        if argv[0] == "gh":
            return subprocess.CompletedProcess(argv, 0, "2026-10-19T21:20:03Z\n", "")
        return real(argv, **kwargs)

    stamp = {
        "GIT_AUTHOR_DATE": "@1792441723 +0000",
        "GIT_COMMITTER_DATE": "@1792441723 +0000",
        "GIT_AUTHOR_NAME": "a",
        "GIT_AUTHOR_EMAIL": "a@b",
        "GIT_COMMITTER_NAME": "a",
        "GIT_COMMITTER_EMAIL": "a@b",
        "PATH": "/usr/bin:/bin",
    }
    for args in (("init", "-q"), ("commit", "-q", "--allow-empty", "-m", "Queue built (#4864)")):
        real(["git", "-C", str(tmp_path), *args], check=True, capture_output=True, env=stamp)
    real(["git", "-C", str(tmp_path), "update-ref", "refs/remotes/origin/dev", "HEAD"], check=True)
    monkeypatch.setattr(ledger_host.subprocess, "run", run)
    assert ledger_host.newest_on_dev(tmp_path, 1_792_445_763) == {"newest": "Queue built (#4864)", "merged_minutes": 16}


class Git:
    def __init__(self, stdout="", code=0, error=None):
        self.stdout, self.code, self.error, self.calls = stdout, code, error, []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if self.error:
            raise self.error
        return subprocess.CompletedProcess(argv, self.code, self.stdout, "")


@pytest.mark.parametrize(
    "git",
    [
        Git(error=FileNotFoundError("git")),
        Git(error=subprocess.TimeoutExpired("git", 5)),
        Git("abc123\torigin/dev@{1000000000}\tA subject", code=128),
        Git("abc123\torigin/dev@{not a stamp}\tA subject"),
        Git("abc123\t1000000000\tA subject"),
        Git(""),
    ],
)
def test_newest_on_dev_is_unknown_when_git_cannot_say(monkeypatch, git):
    monkeypatch.setattr(ledger_host, "merged_at", lambda repo, sha: 1_000_000_000)
    monkeypatch.setattr(ledger_host.subprocess, "run", git)
    assert ledger_host.newest_on_dev(Path("/repo"), 1_000_000_600) == {"newest": None, "merged_minutes": None}
    assert ledger_host.NO_DEV == {"newest": None, "merged_minutes": None}


def test_newest_on_dev_reads_the_dev_tip_with_a_timeout_and_keeps_tabs_in_the_subject(monkeypatch):
    git = Git("abc123\torigin/dev@{1000000000}\tKeep\ttabs\n")
    monkeypatch.setattr(ledger_host.subprocess, "run", git)
    monkeypatch.setattr(ledger_host, "merged_at", lambda repo, sha: None)
    assert ledger_host.newest_on_dev(Path("/repo"), 1_000_000_659) == {"newest": "Keep\ttabs", "merged_minutes": 10}
    [(argv, kwargs)] = git.calls
    assert argv == [
        "git",
        "-C",
        "/repo",
        "log",
        "-g",
        "-1",
        "--date=unix",
        "--format=%H%x09%gd%x09%s",
        "refs/remotes/origin/dev",
    ]
    assert kwargs == {"capture_output": True, "text": True, "timeout": ledger_host.GIT_TIMEOUT_S}
    assert ledger_host.GIT_TIMEOUT_S == 5


def test_newest_on_dev_asks_github_for_the_merge_of_the_dev_tip(monkeypatch):
    asked = []
    monkeypatch.setattr(ledger_host.subprocess, "run", Git("abc123\torigin/dev@{1000000000}\tMerged\n"))
    monkeypatch.setattr(ledger_host, "merged_at", lambda repo, sha: asked.append((repo, sha)) or 1_000_000_300.0)
    assert ledger_host.newest_on_dev(Path("/repo"), 1_000_000_659) == {"newest": "Merged", "merged_minutes": 5}
    assert asked == [(Path("/repo"), "abc123")]


@pytest.mark.parametrize(
    ("gh", "expected"),
    [
        (Git("2026-10-19T21:20:03Z\n"), 1_792_444_803.0),
        (Git("2026-10-19T21:20:03Z\n", code=1), None),
        (Git("\n"), None),
        (Git(error=FileNotFoundError("gh")), None),
        (Git(error=subprocess.TimeoutExpired("gh", 10)), None),
    ],
)
def test_merged_at_reads_the_pull_request_merge_time_of_a_commit(monkeypatch, gh, expected):
    monkeypatch.setattr(ledger_host.subprocess, "run", gh)
    assert ledger_host.merged_at(Path("/repo"), "abc123") == expected
    [(argv, kwargs)] = gh.calls
    assert argv == ["gh", "api", "repos/{owner}/{repo}/commits/abc123/pulls", "--jq", ledger_host.MERGED_JQ]
    assert kwargs == {"cwd": Path("/repo"), "capture_output": True, "text": True, "timeout": ledger_host.GH_TIMEOUT_S}
    assert ledger_host.GH_TIMEOUT_S == 10
    assert ledger_host.MERGED_JQ == 'map(.merged_at | values) | first // ""'


def test_facts_join_the_server_named_in_the_ledger_folder_and_dev(monkeypatch, tmp_path):
    (tmp_path / ".server.pid").write_text("4242")
    monkeypatch.setenv("LEDGER_DIR", str(tmp_path))
    monkeypatch.setattr(ledger_host, "server", lambda pid: {"cpu": pid, "started_minutes": 1})
    monkeypatch.setattr(ledger_host, "newest_on_dev", lambda: {"newest": "x", "merged_minutes": 2})
    assert ledger_host.facts() == {"cpu": 4242, "started_minutes": 1, "newest": "x", "merged_minutes": 2}
