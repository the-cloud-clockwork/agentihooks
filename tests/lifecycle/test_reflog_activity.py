import io
import os
from pathlib import Path

import pytest

from hooks.lifecycle.gitstate import inspect
from hooks.lifecycle.lease import admin_dir
from hooks.lifecycle.worktrees import classify

from .conftest import age, git, snap


@pytest.mark.parametrize(
    "command",
    [("reflog", "expire", "--all", "--expire=never", "--expire-unreachable=never"), ("gc", "--no-prune")],
)
def test_reflog_expiry_does_not_renew_idle_worktree(repo, command):
    path = repo.worktree("expired-log")
    age(path, 3 * 3600)
    log = admin_dir(path) / "logs" / "HEAD"
    old_content = log.read_bytes()
    old_mtime = log.stat().st_mtime

    git(path, *command)

    assert log.read_bytes() == old_content
    assert log.stat().st_mtime > old_mtime
    state = inspect(path, snap().now)
    assert state.last_touch == old_mtime
    finding = classify(path, repo.root(), snap())
    assert (finding.action, finding.reason) == ("remove", "clean and on a remote")


def test_recent_reflog_event_keeps_worktree_with_old_file_mtimes(repo):
    path = repo.worktree("recent-event")
    age(path, 3 * 3600)
    log = admin_dir(path) / "logs" / "HEAD"
    old_mtime = log.stat().st_mtime
    header, message = log.read_text().splitlines()[-1].split("\t", 1)
    identity, _, zone = header.rsplit(" ", 2)
    log.write_text(f"{identity} {int(snap().now) - 60} {zone}\t{message}\n")
    os.utime(log, (old_mtime, old_mtime))

    finding = classify(path, repo.root(), snap())
    assert (finding.action, finding.reason) == ("keep", "clean, recent")


@pytest.mark.parametrize("name", ["index", "HEAD"])
def test_recent_git_activity_still_keeps_worktree(repo, name):
    path = repo.worktree("recent-git")
    age(path, 3 * 3600)
    now = snap().now
    os.utime(admin_dir(path) / name, (now, now))

    finding = classify(path, repo.root(), snap())
    assert (finding.action, finding.reason) == ("keep", "clean, recent")


@pytest.mark.parametrize("content", ["", "broken", "identity invalid +0000\tmessage\n", "broken 1 +0000\n"])
def test_unusable_reflog_keeps_its_file_activity(repo, content):
    path = repo.worktree("unusable-log")
    age(path, 3 * 3600)
    log = admin_dir(path) / "logs" / "HEAD"
    log.write_text(content)

    finding = classify(path, repo.root(), snap())
    assert (finding.action, finding.reason) == ("keep", "clean, recent")


def test_missing_reflog_uses_remaining_git_activity(repo):
    path = repo.worktree("missing-log")
    age(path, 3 * 3600)
    (admin_dir(path) / "logs" / "HEAD").unlink()

    finding = classify(path, repo.root(), snap())
    assert (finding.action, finding.reason) == ("remove", "clean and on a remote")


def test_unreadable_reflog_keeps_its_file_activity(repo, monkeypatch):
    path = repo.worktree("unreadable-log")
    age(path, 3 * 3600)
    log = admin_dir(path) / "logs" / "HEAD"
    now = snap().now
    os.utime(log, (now, now))
    read_text = Path.read_text

    def read(target, *args, **kwargs):
        if target == log:
            raise PermissionError
        return read_text(target, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    finding = classify(path, repo.root(), snap())
    assert (finding.action, finding.reason) == ("keep", "clean, recent")


@pytest.mark.parametrize("recent_first", [False, True])
def test_newest_reflog_event_is_used_even_when_entries_are_not_chronological(repo, recent_first):
    path = repo.worktree("mixed-events")
    age(path, 3 * 3600)
    log = admin_dir(path) / "logs" / "HEAD"
    old_mtime = log.stat().st_mtime
    old_entry = log.read_text().splitlines()[-1]
    header, _, message = old_entry.partition("\t")
    identity, _, zone = header.rsplit(" ", 2)
    recent_entry = f"{identity} {int(snap().now) - 60} {zone}\t{message}"
    entries = [recent_entry, old_entry] if recent_first else [old_entry, recent_entry]
    log.write_text("\n".join(entries) + "\n")
    os.utime(log, (old_mtime, old_mtime))

    finding = classify(path, repo.root(), snap())
    assert (finding.action, finding.reason) == ("keep", "clean, recent")


def test_reflog_activity_is_independent_of_default_encoding(repo, monkeypatch):
    monkeypatch.setenv("GIT_COMMITTER_NAME", "José")
    path = repo.worktree("unicode-event")
    assert "José".encode("utf-8") in (admin_dir(path) / "logs" / "HEAD").read_bytes()
    age(path, 3 * 3600)
    git(path, "reflog", "expire", "--all", "--expire=never", "--expire-unreachable=never")
    original = io.text_encoding

    def encoding(value, *args):
        return "ascii" if value is None else original(value, *args)

    monkeypatch.setattr(io, "text_encoding", encoding)
    with pytest.raises(UnicodeDecodeError):
        (admin_dir(path) / "logs" / "HEAD").read_text()
    finding = classify(path, repo.root(), snap())
    assert (finding.action, finding.reason) == ("remove", "clean and on a remote")
