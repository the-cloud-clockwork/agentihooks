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


def test_the_newest_commit_on_dev_names_its_subject_and_age(tmp_path):
    def git(*args, **env):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True, env=env or None)

    stamp = {
        "GIT_AUTHOR_DATE": "@1000000000 +0000",
        "GIT_COMMITTER_DATE": "@1000000000 +0000",
        "GIT_AUTHOR_NAME": "a",
        "GIT_AUTHOR_EMAIL": "a@b",
        "GIT_COMMITTER_NAME": "a",
        "GIT_COMMITTER_EMAIL": "a@b",
        "PATH": "/usr/bin:/bin",
    }
    git("init", "-q")
    git("commit", "-q", "--allow-empty", "-m", "Time the ledger every pass", **stamp)
    assert ledger_host.newest_on_dev(tmp_path, 1_000_000_600) == {"newest": None, "merged_minutes": None}
    git("update-ref", "refs/remotes/origin/dev", "HEAD")
    assert ledger_host.newest_on_dev(tmp_path, 1_000_000_600) == {
        "newest": "Time the ledger every pass",
        "merged_minutes": 10,
    }


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
        Git("1000000000\tA subject", code=128),
        Git("not a stamp\tA subject"),
    ],
)
def test_newest_on_dev_is_unknown_when_git_cannot_say(monkeypatch, git):
    monkeypatch.setattr(ledger_host.subprocess, "run", git)
    assert ledger_host.newest_on_dev(Path("/repo"), 1_000_000_600) == {"newest": None, "merged_minutes": None}
    assert ledger_host.NO_DEV == {"newest": None, "merged_minutes": None}


def test_newest_on_dev_reads_the_dev_tip_with_a_timeout_and_keeps_tabs_in_the_subject(monkeypatch):
    git = Git("1000000000\tKeep\ttabs\n")
    monkeypatch.setattr(ledger_host.subprocess, "run", git)
    assert ledger_host.newest_on_dev(Path("/repo"), 1_000_000_659) == {"newest": "Keep\ttabs", "merged_minutes": 10}
    [(argv, kwargs)] = git.calls
    assert argv == ["git", "-C", "/repo", "log", "-1", "--format=%ct%x09%s", "origin/dev"]
    assert kwargs == {"capture_output": True, "text": True, "timeout": ledger_host.GIT_TIMEOUT_S}
    assert ledger_host.GIT_TIMEOUT_S == 5


def test_facts_join_the_server_named_in_the_ledger_folder_and_dev(monkeypatch, tmp_path):
    (tmp_path / ".server.pid").write_text("4242")
    monkeypatch.setenv("LEDGER_DIR", str(tmp_path))
    monkeypatch.setattr(ledger_host, "server", lambda pid: {"cpu": pid, "started_minutes": 1})
    monkeypatch.setattr(ledger_host, "newest_on_dev", lambda: {"newest": "x", "merged_minutes": 2})
    assert ledger_host.facts() == {"cpu": 4242, "started_minutes": 1, "newest": "x", "merged_minutes": 2}
