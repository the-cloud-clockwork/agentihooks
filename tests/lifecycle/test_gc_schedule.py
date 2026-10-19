import json
import os
import subprocess
from unittest.mock import Mock

import pytest

pytestmark = pytest.mark.unit

SCHEDULED = "[OK] Lifecycle sweep scheduled hourly on the agentihooks scheduler (0 * * * *)"
REMOVED_UNITS = "[OK] Removed the systemd agentihooks-gc.timer"


@pytest.fixture
def scheduler(monkeypatch):
    import fakeredis

    from hooks.lifecycle import maintenance, timer
    from scripts.scheduler.core import Scheduler

    live = Scheduler(fakeredis.FakeRedis(decode_responses=True), machine="proof")
    monkeypatch.setattr(timer, "_scheduler", lambda: live)
    monkeypatch.setattr(maintenance, "load_roots", lambda: [])
    monkeypatch.setattr("scripts.scheduler.service.ensure_running", Mock(return_value=True))
    return live


@pytest.fixture
def systemctl(monkeypatch):
    from hooks.lifecycle import timer

    calls = []
    monkeypatch.setattr(timer, "_systemctl", lambda *args: calls.append(args) or True)
    monkeypatch.setattr(timer, "_live_home", lambda: True)
    return calls


def _legacy_units(*suffixes):
    from hooks.lifecycle import timer

    folder = timer.unit_dir()
    folder.mkdir(parents=True, exist_ok=True)
    for suffix in suffixes:
        (folder / f"{timer.UNIT}.{suffix}").write_text("[Unit]\n", encoding="utf-8")
    return folder


def test_install_schedules_one_hourly_cron_connected_to_the_gc_slot(scheduler):
    from hooks.lifecycle import timer
    from scripts.scheduler import service

    assert timer.install_timer("/venv/bin/python", "/opt/agentihooks") == SCHEDULED
    assert timer.install_timer("/venv/bin/python", "/opt/agentihooks") == SCHEDULED
    assert service.ensure_running.call_count == 2
    started_with = service.ensure_running.call_args.args[0]
    assert started_with["AGENTIHOOKS_SWARM_REDIS_URL"] == os.environ["AGENTIHOOKS_SWARM_REDIS_URL"]
    [entry_id] = timer.scheduled(scheduler)
    record = scheduler.get(entry_id).record
    assert record["kind"] == "cron" and record["expression"] == "0 * * * *" and record["state"] == "active"
    assert record["owner"] == "agentihooks-gc"
    from hooks.config import AGENTIHOOKS_HOME

    assert json.loads(record["payload"]) == {
        "python": "/venv/bin/python",
        "root": "/opt/agentihooks",
        "home": str(AGENTIHOOKS_HOME),
    }
    assert [{key: slot[key] for key in ("signal", "kind", "reference")} for slot in record["slots"]] == [
        {"signal": "timeout", "kind": "callable", "reference": "hooks.lifecycle.timer:sweep"}
    ]
    assert not (timer.unit_dir() / "agentihooks-gc.timer").exists()
    assert not (timer.unit_dir() / "agentihooks-gc.service").exists()


def test_install_starts_the_runner_on_the_default_redis_url(scheduler, monkeypatch):
    from hooks.lifecycle import timer
    from scripts.scheduler import service
    from scripts.swarm.store import DEFAULT_URL

    for name in ("REDIS_URL", "AGENTIHOOKS_HIVE_REDIS_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENTIHOOKS_SWARM_REDIS_URL", "")
    monkeypatch.setenv("AGENTIHOOKS_PROOF", "kept")
    timer.install_timer("/venv/bin/python", "/opt/agentihooks")
    started_with = service.ensure_running.call_args.args[0]
    assert started_with["AGENTIHOOKS_SWARM_REDIS_URL"] == DEFAULT_URL
    assert started_with["AGENTIHOOKS_PROOF"] == "kept"


@pytest.mark.parametrize("suffixes", [("timer", "service"), ("service",)])
def test_install_disables_and_deletes_a_legacy_systemd_timer(scheduler, systemctl, suffixes):
    from hooks.lifecycle import timer

    folder = _legacy_units(*suffixes)
    assert timer.install_timer("/venv/bin/python", "/opt/agentihooks") == f"{REMOVED_UNITS}\n  {SCHEDULED}"
    assert not list(folder.glob("agentihooks-gc.*"))
    assert systemctl == [("disable", "--now", "agentihooks-gc.timer"), ("daemon-reload",)]


def test_legacy_units_outside_the_live_home_are_deleted_without_systemctl(scheduler, systemctl, monkeypatch):
    from hooks.lifecycle import timer

    monkeypatch.setattr(timer, "_live_home", lambda: False)
    folder = _legacy_units("timer", "service")
    assert timer.remove_timer() == REMOVED_UNITS
    assert not list(folder.glob("agentihooks-gc.*"))
    assert systemctl == []


def test_remove_timer_cancels_the_cron(scheduler, systemctl):
    from hooks.lifecycle import timer

    timer.install_timer("/venv/bin/python", "/opt/agentihooks")
    [entry_id] = timer.scheduled(scheduler)
    assert timer.remove_timer() == "[OK] Lifecycle sweep removed from the agentihooks scheduler"
    assert scheduler.get(entry_id).state == "cancelled"
    assert timer.scheduled(scheduler) == []
    assert timer.remove_timer() == "[--] agentihooks-gc not scheduled"
    assert systemctl == []


def test_remove_timer_reports_the_units_and_the_cron_together(scheduler, systemctl):
    from hooks.lifecycle import timer

    timer.install_timer("/venv/bin/python", "/opt/agentihooks")
    _legacy_units("timer")
    assert timer.remove_timer() == (f"{REMOVED_UNITS}\n  [OK] Lifecycle sweep removed from the agentihooks scheduler")


def test_the_scheduler_uses_the_swarm_redis_client(monkeypatch):
    import fakeredis

    from hooks.lifecycle import timer

    client = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr("scripts.swarm.store.redis_client", lambda: client)
    assert timer._scheduler().redis is client


def test_gc_cli_parses_the_schedule_flags(monkeypatch, capsys):
    from scripts import gc_cli

    parser = gc_cli._parser()
    args = parser.parse_args(["gc", "--install-timer", "--remove-timer"])
    assert args.install_timer is True and args.remove_timer is True
    args = parser.parse_args(["gc"])
    assert args.install_timer is False and args.remove_timer is False
    monkeypatch.setenv("COLUMNS", "200")
    with pytest.raises(SystemExit):
        parser.parse_args(["gc", "--help"])
    shown = capsys.readouterr().out
    assert " Schedule the hourly sweep on the agentihooks scheduler\n" in shown
    assert " Remove the hourly sweep from the agentihooks scheduler\n" in shown


def test_scheduled_lists_only_live_gc_entries(scheduler):
    from hooks.lifecycle import timer

    def entry(state, owner="agentihooks-gc"):
        made = scheduler.timer(owner=owner, payload="{}", delay=60, now=0)
        scheduler.update(made.id, lambda record: record.update(state=state))
        return made.id

    live = {entry("ready"), entry("active"), entry("pending")}
    for state in ("cancelled", "fired", "stopped"):
        entry(state)
    entry("active", owner="someone-else")
    assert set(timer.scheduled(scheduler)) == live


def test_unreachable_redis_reports_and_starts_no_runner(monkeypatch, systemctl):
    from redis.exceptions import ConnectionError

    from hooks.lifecycle import maintenance, timer

    def refused():
        raise ConnectionError("refused")

    running = Mock()
    monkeypatch.setattr(timer, "_scheduler", refused)
    monkeypatch.setattr(maintenance, "load_roots", lambda: [])
    monkeypatch.setattr("scripts.scheduler.service.ensure_running", running)
    assert timer.install_timer("/venv/bin/python", "/opt/agentihooks") == (
        "[--] Redis unreachable: lifecycle sweep not scheduled, sessions trigger the sweep instead"
    )
    running.assert_not_called()
    _legacy_units("timer")
    assert timer.remove_timer() == f"{REMOVED_UNITS}\n  [--] Redis unreachable: lifecycle sweep schedule not checked"


def test_the_gc_slot_starts_one_detached_sweep_per_delivery(monkeypatch):
    import fakeredis

    from hooks._redis import redis_key
    from hooks.lifecycle import timer

    redis = fakeredis.FakeRedis(decode_responses=True)
    started = Mock()
    monkeypatch.setattr(timer.subprocess, "Popen", started)
    monkeypatch.setattr(timer.shutil, "which", lambda name: None)
    monkeypatch.setenv("AGENTIHOOKS_PROOF", "kept")
    payload = {"python": "/venv/bin/python", "root": "/opt/agentihooks", "home": "/srv/ah"}
    event = {"payload": json.dumps(payload)}
    timer.sweep(event, "delivery-1", redis)
    timer.sweep(event, "delivery-1", redis)
    started.assert_called_once()
    assert started.call_args.args == (
        ["nice", "-n", "10", "/venv/bin/python", "-m", "scripts.gc_cli", "gc", "--enforce"],
    )
    kwargs = dict(started.call_args.kwargs)
    env = kwargs.pop("env")
    assert env["AGENTIHOOKS_HOME"] == "/srv/ah" and env["AGENTIHOOKS_PROOF"] == "kept"
    assert kwargs == {
        "cwd": "/opt/agentihooks",
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "start_new_session": True,
    }
    marker = redis_key("scheduler", "agentihooks-gc:delivery-1")
    assert redis.get(marker) == "1" and redis.ttl(marker) == 86400
    timer.sweep(event, "delivery-2", redis)
    assert started.call_count == 2


def test_the_sweep_is_time_limited_and_idle_where_the_tools_exist(monkeypatch):
    import fakeredis

    from hooks.lifecycle import timer

    started = Mock()
    monkeypatch.setattr(timer.subprocess, "Popen", started)
    monkeypatch.setattr(timer.shutil, "which", {"timeout": "/usr/bin/timeout", "ionice": "/usr/bin/ionice"}.get)
    payload = {"python": "/venv/bin/python", "root": "/opt/agentihooks", "home": "/srv/ah"}
    timer.sweep({"payload": json.dumps(payload)}, "delivery-1", fakeredis.FakeRedis())
    assert started.call_args.args[0] == [
        "timeout",
        "3600",
        "nice",
        "-n",
        "10",
        "ionice",
        "-c",
        "3",
        "/venv/bin/python",
        "-m",
        "scripts.gc_cli",
        "gc",
        "--enforce",
    ]


def test_a_sweep_that_cannot_start_never_crashes_the_runner(monkeypatch):
    import fakeredis

    from hooks.lifecycle import timer

    logged = Mock()
    monkeypatch.setattr("hooks.common.log", logged)
    monkeypatch.setattr(timer.subprocess, "Popen", Mock(side_effect=FileNotFoundError("nice")))
    payload = {"python": "/gone/python", "root": "/opt/agentihooks", "home": "/srv/ah"}
    assert timer.sweep({"payload": json.dumps(payload)}, "delivery-1", fakeredis.FakeRedis()) is None
    logged.assert_called_once_with("lifecycle sweep failed to start", {"error": "nice"})


def test_the_runner_fires_the_sweep_on_the_hour_and_reschedules(scheduler, monkeypatch):
    from hooks.lifecycle import timer

    timer.install_timer("/venv/bin/python", "/opt/agentihooks")
    started = Mock()
    monkeypatch.setattr(timer.subprocess, "Popen", started)
    [entry_id] = timer.scheduled(scheduler)
    due = scheduler.get(entry_id).record["due"]
    assert due % 3600 == 0
    assert scheduler.run_due(now=due - 1) == 0
    assert scheduler.run_due(now=due) == 1
    assert scheduler.run_due(now=due + 1) == 0
    started.assert_called_once()
    record = scheduler.get(entry_id).record
    assert record["state"] == "active" and record["due"] == due + 3600
