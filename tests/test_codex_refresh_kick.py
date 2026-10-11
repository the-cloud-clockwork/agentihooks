import os
import time

import pytest

from hooks.lifecycle import codex_refresh_kick as kick

FLAG = {"AGENTIHOOKS_AICODE_TOKEN_REFRESH": "true"}


@pytest.fixture
def spawned(monkeypatch):
    calls = []
    monkeypatch.setattr(kick.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(kick.subprocess, "Popen", lambda argv, **kwargs: calls.append((argv, kwargs)))
    return calls


def _stamp(home, hours_old):
    stamp = home / "codex-refresh.stamp"
    stamp.touch()
    at = time.time() - hours_old * 3600
    os.utime(stamp, (at, at))
    return stamp


def test_a_machine_that_is_not_the_refresh_authority_pays_nothing(tmp_path, spawned):
    assert kick.kick(tmp_path, {}) is False
    assert kick.kick(tmp_path, {"AGENTIHOOKS_AICODE_TOKEN_REFRESH": "false"}) is False
    assert not spawned and not (tmp_path / "codex-refresh.stamp").exists()


def test_a_fresh_stamp_starts_nothing(tmp_path, spawned):
    _stamp(tmp_path, 5)
    assert kick.kick(tmp_path, FLAG) is False
    assert not spawned


def test_a_stale_or_missing_stamp_touches_it_and_starts_one_detached_refresh(tmp_path, spawned):
    stamp = _stamp(tmp_path, 7)
    assert kick.kick(tmp_path, FLAG) is True
    assert time.time() - stamp.stat().st_mtime < 60
    stamp.unlink()
    assert kick.kick(tmp_path, FLAG) is True
    assert stamp.exists()
    assert [argv for argv, _ in spawned] == [["/usr/bin/agentihooks", "codex", "refresh"]] * 2
    assert all(kwargs["start_new_session"] for _, kwargs in spawned)


def test_the_interval_comes_from_the_environment(tmp_path, spawned):
    _stamp(tmp_path, 2)
    assert kick.kick(tmp_path, {**FLAG, "AGENTIHOOKS_AICODE_TOKEN_REFRESH_INTERVAL_H": "1"}) is True
    assert len(spawned) == 1


def test_a_failure_to_start_is_swallowed_and_leaves_no_stamp(tmp_path, monkeypatch):
    monkeypatch.setattr(kick.shutil, "which", lambda name: "/usr/bin/agentihooks")

    def boom(argv, **kwargs):
        raise OSError("no fork")

    monkeypatch.setattr(kick.subprocess, "Popen", boom)
    assert kick.kick(tmp_path, FLAG) is False
    assert not (tmp_path / "codex-refresh.stamp").exists()
