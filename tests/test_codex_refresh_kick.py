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


def test_a_missing_agentihooks_binary_is_logged_and_leaves_no_stamp(tmp_path, monkeypatch):
    logged = []
    monkeypatch.setattr(kick.shutil, "which", lambda name: None)
    monkeypatch.setattr(kick, "log", lambda message, data: logged.append((message, data)))
    assert kick.kick(tmp_path, FLAG) is False
    assert logged == [("codex refresh kick skipped: agentihooks is not on PATH", {})]
    assert not (tmp_path / "codex-refresh.stamp").exists()


NOW = 1_800_000_000.0


def _aged(home, seconds, monkeypatch):
    monkeypatch.setattr(kick.time, "time", lambda: NOW)
    stamp = home / "codex-refresh.stamp"
    stamp.touch()
    os.utime(stamp, (NOW - seconds, NOW - seconds))


def test_the_child_is_detached_from_every_stream(tmp_path, spawned):
    assert kick.kick(tmp_path, FLAG) is True
    assert spawned == [
        (
            ["/usr/bin/agentihooks", "codex", "refresh"],
            {
                "stdin": kick.subprocess.DEVNULL,
                "stdout": kick.subprocess.DEVNULL,
                "stderr": kick.subprocess.DEVNULL,
                "start_new_session": True,
            },
        )
    ]


def test_a_missing_home_folder_is_created(tmp_path, spawned):
    home = tmp_path / "a" / "b"
    assert kick.kick(home, FLAG) is True
    assert (home / "codex-refresh.stamp").exists()


@pytest.mark.parametrize(
    "interval, age, due",
    [("1", 3600, True), ("1", 3599.5, False), ("1", 3600.5, True), ("", 6 * 3600, True), ("soon", 5 * 3600, False)]
    + [("soon", 6 * 3600, True)],
)
def test_the_stamp_is_due_at_exactly_the_interval(interval, age, due, tmp_path, spawned, monkeypatch):
    _aged(tmp_path, age, monkeypatch)
    environ = {**FLAG, "AGENTIHOOKS_AICODE_TOKEN_REFRESH_INTERVAL_H": interval}
    assert kick.kick(tmp_path, environ) is due


def test_a_failure_is_logged_with_its_error_and_a_quiet_node_logs_nothing(tmp_path, monkeypatch):
    logged = []
    monkeypatch.setattr(kick, "log", lambda message, data: logged.append((message, data)))
    monkeypatch.setattr(kick.shutil, "which", lambda name: "/usr/bin/agentihooks")

    def boom(argv, **kwargs):
        raise OSError("no fork")

    monkeypatch.setattr(kick.subprocess, "Popen", boom)
    assert kick.kick(tmp_path, {}) is False
    assert logged == []
    assert kick.kick(tmp_path, FLAG) is False
    assert logged == [("codex refresh kick failed", {"error": "no fork"})]
