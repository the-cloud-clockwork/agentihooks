import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from hooks.lifecycle import maintenance, timer
from hooks.lifecycle.run import sweep
from scripts import gc_cli

from .conftest import git, snap


@pytest.fixture(autouse=True)
def _global_config(monkeypatch):
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def _registered() -> list[str]:
    result = subprocess.run(
        ["git", "config", "--global", "--get-all", "maintenance.repo"], capture_output=True, text=True
    )
    return [str(Path(line).resolve()) for line in result.stdout.splitlines()]


def _objects(path) -> dict:
    counts = dict(line.split(": ", 1) for line in git(path, "count-objects", "-v").splitlines())
    return {"loose": int(counts["count"]), "packs": int(counts["packs"])}


def test_an_enforcing_sweep_registers_a_checkout_lacking_maintenance_and_reports_its_objects(repo, tmp_path):
    repo.worktree("held")
    assert str(repo.primary.resolve()) not in _registered()
    report = sweep([repo.root()], snap(), tmp_path / "state", act=True)
    assert str(repo.primary.resolve()) in _registered()
    assert report["git_maintenance"] == [
        {"path": str(repo.primary), "registered": True, "outcome": "registered", **_objects(repo.primary)}
    ]
    again = sweep([repo.root()], snap(), tmp_path / "state", act=True)
    assert again["git_maintenance"][0]["outcome"] == ""
    assert _registered().count(str(repo.primary.resolve())) == 1


def test_a_report_only_sweep_counts_objects_without_registering(repo, tmp_path):
    repo.worktree("held")
    report = sweep([repo.root()], snap(), tmp_path / "state")
    entry = report["git_maintenance"][0]
    assert entry["registered"] is False and entry["outcome"] == ""
    assert entry["loose"] == _objects(repo.primary)["loose"] > 0
    assert str(repo.primary.resolve()) not in _registered()


def test_lifecycle_setup_registers_every_managed_primary(repo, monkeypatch):
    repo.worktree("held")
    monkeypatch.setattr(maintenance, "load_roots", lambda: [repo.root()])
    from redis.exceptions import ConnectionError as Refused

    monkeypatch.setattr(timer, "_scheduler", Mock(side_effect=Refused("refused")))
    message = timer.install_timer("/venv/bin/python", "/opt/agentihooks")
    assert str(repo.primary.resolve()) in _registered()
    assert message.endswith(f"\n  [OK] git maintenance registered for {repo.primary}")
    assert "git maintenance" not in timer.install_timer("/venv/bin/python", "/opt/agentihooks")


def test_the_live_home_schedules_maintenance_and_reports_a_failed_start(repo, monkeypatch):
    calls = []

    def fake(path, *args, timeout=120):
        calls.append(args)
        failed = args[0] in ("maintenance", "count-objects")
        return subprocess.CompletedProcess(args, 1 if failed else 0, "", "no scheduler" if failed else "")

    monkeypatch.setattr(maintenance, "_live_home", lambda: True)
    monkeypatch.setattr(maintenance, "git", fake)
    report = maintenance.maintain([repo.primary], act=True)
    assert ("maintenance", "start") in calls and ("maintenance", "register") not in calls
    assert report[0]["registered"] is False and report[0]["outcome"] == "failed: no scheduler"
    assert report[0]["loose"] is None and report[0]["packs"] is None


def test_a_git_error_is_reported_without_aborting_the_sweep(repo, monkeypatch):
    def broken(path, *args, timeout=120):
        if args[0] in ("maintenance", "config"):
            raise subprocess.TimeoutExpired(["git", *args], timeout)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(maintenance, "git", broken)
    report = maintenance.maintain([repo.primary], act=True)
    assert report[0]["registered"] is False and report[0]["outcome"].startswith("failed: ")
    assert report[0]["loose"] is None and report[0]["packs"] is None


def test_object_counts_come_from_the_count_objects_fields(repo, monkeypatch):
    output = "count: 78532\nsize: 900\nin-pack: 12\npacks: 47\nsize-pack: 3\nprune-packable: 0\ngarbage: 0\n"
    monkeypatch.setattr(
        maintenance, "git", lambda path, *args, timeout=120: subprocess.CompletedProcess(args, 0, output, "")
    )
    assert maintenance.objects(repo.primary) == {"loose": 78532, "packs": 47}


def test_registered_repos_read_every_global_entry_and_ignore_local_config(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    git(home, "init", "-q")
    git(home, "config", "maintenance.repo", "/local")
    for path in ("/first", "/second"):
        git(home, "config", "--global", "--add", "maintenance.repo", path)
    assert maintenance.registered_repos() == ["/first", "/second"]


def test_registered_repos_ask_git_for_every_global_maintenance_entry(monkeypatch):
    calls = []

    def fake(path, *args, timeout=120):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "/a\n/b\n", "")

    monkeypatch.setattr(maintenance, "git", fake)
    assert maintenance.registered_repos() == ["/a", "/b"]
    assert calls == [("config", "--global", "--get-all", "maintenance.repo")]


def test_only_linked_worktrees_name_a_primary(repo, tmp_path):
    held = repo.worktree("held")
    submodule = tmp_path / "sub"
    submodule.mkdir()
    (submodule / ".git").write_text(f"gitdir: {repo.primary}/.git/modules/sub\n")
    assert maintenance.primaries([held, repo.primary, submodule]) == [repo.primary]


def test_a_scoped_sweep_reports_only_checkouts_inside_its_scope(repo, tmp_path):
    repo.worktree("held")
    inside = sweep([repo.root()], snap(), tmp_path / "state", scope=str(repo.primary))
    outside = sweep([repo.root()], snap(), tmp_path / "state", scope=str(tmp_path / "elsewhere"))
    assert [item["path"] for item in inside["git_maintenance"]] == [str(repo.primary)]
    assert outside["git_maintenance"] == []


def test_setup_marks_a_failed_registration(monkeypatch):
    monkeypatch.setattr(maintenance, "load_roots", lambda: [])
    monkeypatch.setattr(maintenance, "discover", lambda roots: [])
    failed = [{"path": "/r/a", "registered": False, "outcome": "failed: no git", "loose": None, "packs": None}]
    monkeypatch.setattr(maintenance, "maintain", lambda paths, act: failed)
    assert maintenance.setup() == ["[!!] git maintenance failed: no git for /r/a"]


def test_gc_prints_the_object_backlog_per_checkout(monkeypatch, capsys):
    report = {
        "totals": {},
        "findings": [],
        "git_maintenance": [
            {"path": "/r/a", "registered": False, "outcome": "", "loose": 78532, "packs": 47},
            {"path": "/r/b", "registered": True, "outcome": "", "loose": 3, "packs": 1},
        ],
    }
    monkeypatch.setattr(gc_cli, "sweep", lambda scope="", act=False: report)
    assert gc_cli.main(["gc"]) == 0
    out = capsys.readouterr().out
    assert "git       78532 loose    47 packs  /r/a  (maintenance not registered)" in out
    assert "git           3 loose     1 packs  /r/b  (maintenance registered)" in out
