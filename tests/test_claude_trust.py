import json
import os
import time
from typing import Any

import pytest

from scripts import claude_trust


def _config(tmp_path, trusted):
    config = tmp_path / ".claude.json"
    config.write_text(json.dumps({"projects": {str(path): {"hasTrustDialogAccepted": True} for path in trusted}}))
    return config


def _accepted(config, directory):
    return json.loads(config.read_text())["projects"].get(str(directory), {}).get("hasTrustDialogAccepted")


@pytest.mark.parametrize("git_marker", ["dir", "file"])
def test_a_git_repo_under_a_trusted_parent_is_marked(tmp_path, git_marker):
    repo = tmp_path / "work" / "repo"
    repo.mkdir(parents=True)
    if git_marker == "dir":
        (repo / ".git").mkdir()
    else:
        (repo / ".git").write_text("gitdir: elsewhere\n")
    config = _config(tmp_path, [tmp_path / "work"])

    assert claude_trust.ensure_trusted(repo, {"HOME": str(tmp_path)}) == ("marked", "")
    assert _accepted(config, repo) is True


def test_a_subfolder_of_a_trusted_repo_is_left_as_is(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "sub").mkdir()
    config = _config(tmp_path, [repo])
    original = config.read_text()

    assert claude_trust.ensure_trusted(repo / "sub", {"HOME": str(tmp_path)}) == ("trusted", "")
    assert config.read_text() == original


def test_a_plain_folder_under_a_trusted_parent_is_left_as_is(tmp_path):
    folder = tmp_path / "work" / "plain"
    folder.mkdir(parents=True)
    config = _config(tmp_path, [tmp_path / "work"])
    original = config.read_text()

    assert claude_trust.ensure_trusted(folder, {"HOME": str(tmp_path)}) == ("trusted", "")
    assert config.read_text() == original


from pathlib import Path


def test__read_decodes_the_config_as_utf8(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / ".claude.json"
    config.write_text("{}")
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    original = Path.read_text

    def spy(self: Path, *args: Any, **kwargs: Any) -> str:
        calls.append((args, kwargs))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", spy)
    assert claude_trust._read(config) == {}
    assert calls == [((), {"encoding": "utf-8"})]


@pytest.mark.feature("claude-trust-lock-reclaim")
def test_an_abandoned_lock_from_a_killed_launch_is_reclaimed_and_the_folder_marked(tmp_path: Path) -> None:
    folder = tmp_path / "repo"
    (folder / ".git").mkdir(parents=True)
    config = _config(tmp_path, [])
    lock = config.with_name(".claude.json.lock")
    lock.mkdir()
    abandoned = time.time() - 17 * 3600
    os.utime(lock, (abandoned, abandoned))

    assert claude_trust.ensure_trusted(folder, {"HOME": str(tmp_path)}, lock_timeout=0.2) == ("marked", "")
    assert _accepted(config, folder) is True
    assert not lock.exists()


@pytest.mark.feature("claude-trust-lock-reclaim")
def test_a_lock_held_by_a_live_holder_still_blocks_the_mark(tmp_path: Path) -> None:
    folder = tmp_path / "repo"
    (folder / ".git").mkdir(parents=True)
    config = _config(tmp_path, [])
    lock = config.with_name(".claude.json.lock")
    lock.mkdir()

    trust, why = claude_trust.ensure_trusted(folder, {"HOME": str(tmp_path)}, lock_timeout=0.2)

    assert (trust, why) == ("untrusted", f"{lock} is held")
    assert lock.is_dir()
    assert _accepted(config, folder) is None


@pytest.mark.feature("claude-trust-lock-reclaim")
def test_a_lock_just_inside_the_stale_bound_is_left_to_its_holder(tmp_path: Path) -> None:
    config = tmp_path / ".claude.json"
    lock = config.with_name(".claude.json.lock")
    lock.mkdir()
    recent = time.time() - claude_trust.STALE_LOCK_SECONDS + 5
    os.utime(lock, (recent, recent))

    assert claude_trust._reclaim(lock) is False
    assert lock.is_dir()


@pytest.mark.feature("claude-trust-lock-reclaim")
def test_a_lock_past_the_stale_bound_is_removed_and_a_vanished_one_reads_as_free(tmp_path: Path) -> None:
    lock = tmp_path / ".claude.json.lock"
    lock.mkdir()
    old = time.time() - claude_trust.STALE_LOCK_SECONDS - 5
    os.utime(lock, (old, old))

    assert claude_trust._reclaim(lock) is True
    assert not lock.exists()
    assert claude_trust._reclaim(lock) is True


@pytest.mark.feature("claude-trust-lock-reclaim")
def test_a_stale_lock_another_launch_removed_first_reads_as_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = tmp_path / ".claude.json.lock"
    lock.mkdir()
    old = time.time() - claude_trust.STALE_LOCK_SECONDS - 5
    os.utime(lock, (old, old))

    def gone(self: Path) -> None:
        raise FileNotFoundError(self)

    monkeypatch.setattr(Path, "rmdir", gone)

    assert claude_trust._reclaim(lock) is True
