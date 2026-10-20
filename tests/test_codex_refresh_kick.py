import contextlib
import os
import signal
import subprocess
import time
from unittest.mock import Mock, call

import pytest

from hooks.lifecycle import codex_refresh_kick as kick

FLAG = {"AGENTIHOOKS_AICODE_TOKEN_REFRESH": "true"}


@pytest.fixture
def spawned(monkeypatch):
    calls = []
    monkeypatch.setattr(kick.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(kick.subprocess, "Popen", lambda argv, **kwargs: calls.append((argv, kwargs)) or Mock(pid=4242))
    monkeypatch.setattr(kick.os, "killpg", lambda pid, sig: None)
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


@pytest.mark.parametrize("outlives", [False, True], ids=["completed", "timeout"])
@pytest.mark.parametrize("group_gone", [False, True], ids=["live-group", "exited-group"])
def test_the_refresh_is_waited_on_then_its_group_is_killed_and_it_is_reaped(tmp_path, monkeypatch, outlives, group_gone):
    process = Mock(pid=4242)
    process.wait.side_effect = [subprocess.TimeoutExpired("agentihooks", 1.0) if outlives else 0, -signal.SIGKILL]
    killpg = Mock(side_effect=ProcessLookupError if group_gone else None)
    monkeypatch.setattr(kick.shutil, "which", lambda name: "/usr/bin/agentihooks")
    monkeypatch.setattr(kick.subprocess, "Popen", lambda argv, **kwargs: process)
    monkeypatch.setattr(kick.os, "killpg", killpg)
    assert kick.kick(tmp_path, FLAG) is True
    assert process.wait.call_args_list == [call(timeout=1.0), call()]
    killpg.assert_called_once_with(4242, signal.SIGKILL)


@pytest.mark.wall_clock
@pytest.mark.parametrize(
    "script, expected",
    [("sleep 0.1\nexit 3\n", 3), ("sleep 30 &\nwait\n", -signal.SIGKILL), ("sleep 30 &\nexit 0\n", 0)],
    ids=["fast", "slow", "leaves-a-child"],
)
def test_the_refresh_process_group_finishes_before_the_kick_returns(tmp_path, monkeypatch, script, expected):
    exe = tmp_path / "agentihooks"
    exe.write_text(f"#!/bin/sh\n{script}")
    exe.chmod(0o755)
    started = []
    popen = subprocess.Popen
    monkeypatch.setattr(kick.shutil, "which", lambda name: str(exe))
    monkeypatch.setattr(kick.subprocess, "Popen", lambda *a, **kw: started.append(popen(*a, **kw)) or started[-1])
    began = time.monotonic()
    try:
        assert kick.kick(tmp_path, FLAG) is True
        elapsed = time.monotonic() - began
        returncode = started[0].returncode
        deadline = time.monotonic() + 2
        while True:
            rows = subprocess.check_output(["ps", "-eo", "pid=,pgid=,stat="], text=True).splitlines()
            survivors = [
                row for row in rows if int(row.split()[1]) == started[0].pid and not row.split()[2].startswith("Z")
            ]
            if not survivors or time.monotonic() >= deadline:
                break
            time.sleep(0.05)
    finally:
        if started:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(started[0].pid, signal.SIGKILL)
            started[0].wait()
    assert elapsed < 5
    assert returncode == expected
    assert survivors == [], "the refresh process group outlived the session start kick"
