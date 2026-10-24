import json
import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts import gc_cli
from scripts.host_guard import sync

pytestmark = pytest.mark.unit

LINKS = ("guard-daemon.sh", "guard-config.json")
LEGACY = ("guard-daemon.timer", "guard-daemon.service")
HELP = "Link the host process guard to this checkout, or report its drift"


@pytest.fixture
def scheduler(monkeypatch):
    import fakeredis

    from scripts.scheduler.core import Scheduler

    live = Scheduler(fakeredis.FakeRedis(decode_responses=True), machine="proof")
    monkeypatch.setattr(sync, "_scheduler", lambda: live)
    monkeypatch.setattr(sync, "_stranded_scheduler", lambda: None)
    monkeypatch.setattr("scripts.scheduler.service.ensure_running", Mock(return_value=True))
    return live


@pytest.fixture
def stranded(monkeypatch, scheduler):
    import fakeredis

    from scripts.scheduler.core import Scheduler

    old = Scheduler(fakeredis.FakeRedis(decode_responses=True), machine="proof")
    monkeypatch.setattr(sync, "_stranded_scheduler", lambda: old)
    return old


@pytest.fixture
def calls(monkeypatch):
    recorded = []

    def fake(*args):
        recorded.append(args)
        return True

    monkeypatch.setattr(sync, "_systemctl", fake)
    return recorded


@pytest.fixture
def unreachable(monkeypatch):
    from redis.exceptions import ConnectionError

    def refuse():
        raise ConnectionError("refused")

    monkeypatch.setattr(sync, "_scheduler", refuse)


def _units(home: Path) -> Path:
    return home / ".config" / "systemd" / "user"


def _legacy(home: Path) -> None:
    _units(home).mkdir(parents=True, exist_ok=True)
    for name in LEGACY:
        (_units(home) / name).write_text("[Unit]\n")


def _records(scheduler) -> list[dict]:
    return [entry.record for entry in scheduler.owned(sync.OWNER) if entry.state in sync.LIVE]


def _installed(home: Path) -> str:
    return f"[OK] Host guard installed in {home} from {sync.REPO}, every 60s on the agentihooks scheduler"


def test_a_synced_home_reports_no_drift(tmp_path, scheduler, calls):
    sync.install(tmp_path, enable=True)
    assert sync.drift(tmp_path) == []


def test_a_hand_copied_config_is_drift_even_when_its_text_matches(tmp_path, scheduler, calls):
    sync.install(tmp_path, enable=True)
    config = tmp_path / "guard-config.json"
    config.unlink()
    config.write_text((sync.REPO / "guard-config.json").read_text())
    assert sync.drift(tmp_path) == [f"{config} is not a link to {sync.REPO / 'guard-config.json'}"]


def test_a_link_to_another_copy_and_a_missing_script_are_drift(tmp_path, scheduler, calls):
    sync.install(tmp_path, enable=True)
    other = tmp_path / "other.json"
    other.write_text("{}")
    (tmp_path / "guard-config.json").unlink()
    (tmp_path / "guard-config.json").symlink_to(other)
    (tmp_path / "guard-daemon.sh").unlink()
    assert sync.drift(tmp_path) == [
        f"{tmp_path / 'guard-daemon.sh'} is missing",
        f"{tmp_path / 'guard-config.json'} is not a link to {sync.REPO / 'guard-config.json'}",
    ]


def test_a_leftover_systemd_unit_and_a_missing_schedule_are_drift(tmp_path, scheduler, calls):
    for name in LINKS:
        (tmp_path / name).symlink_to(sync.REPO / name)
    _legacy(tmp_path)
    assert sync.drift(tmp_path) == [
        f"{_units(tmp_path) / name} still runs the guard on systemd instead of the agentihooks scheduler"
        for name in LEGACY
    ] + [f"{tmp_path / 'guard-daemon.sh'} is not scheduled every 60s on the agentihooks scheduler"]


def test_a_schedule_for_another_home_is_drift(tmp_path, scheduler, calls):
    other = tmp_path / "other"
    other.mkdir()
    sync.install(other, enable=True)
    for name in LINKS:
        (tmp_path / name).symlink_to(sync.REPO / name)
    assert sync.drift(tmp_path) == [
        f"{tmp_path / 'guard-daemon.sh'} is not scheduled every 60s on the agentihooks scheduler"
    ]


def _scheduled(scheduler, home: Path, every: float, callback) -> None:
    entry = scheduler.interval(
        owner=sync.OWNER, payload=json.dumps({"script": str(home / "guard-daemon.sh")}), every=every
    )
    entry.timeout.connect(callback)
    entry.start()


def _other_callback(event, delivery_id, redis):
    return None


@pytest.mark.parametrize(("every", "callback"), [(300, sync.tick), (60, _other_callback)])
def test_a_schedule_at_another_period_or_callback_is_drift(tmp_path, scheduler, every, callback):
    for name in LINKS:
        (tmp_path / name).symlink_to(sync.REPO / name)
    _scheduled(scheduler, tmp_path, every, callback)
    assert sync.drift(tmp_path) == [
        f"{tmp_path / 'guard-daemon.sh'} is not scheduled every 60s on the agentihooks scheduler"
    ]
    assert sync.reconcile(tmp_path) == _installed(tmp_path)
    [record] = _records(scheduler)
    assert record["delay"] == 60 and record["slots"][0]["reference"] == "scripts.host_guard.sync:tick"


def test_a_failed_reschedule_keeps_the_running_schedule(tmp_path, scheduler, monkeypatch, calls):
    from redis.exceptions import ConnectionError

    sync.install(tmp_path, enable=True)

    def refuse(**options):
        raise ConnectionError("refused")

    monkeypatch.setattr(scheduler, "interval", refuse)
    assert sync.install(tmp_path, enable=True).startswith("[!!]")
    assert len(_records(scheduler)) == 1


def test_reconcile_starts_the_scheduler_for_a_guard_in_sync(tmp_path, scheduler, monkeypatch, calls):
    from scripts.scheduler import service
    from scripts.swarm import store

    sync.install(tmp_path, enable=True)
    service.ensure_running.reset_mock()
    monkeypatch.setattr(store, "redis_url", lambda environ: "redis://proof:6379/0")
    assert sync.reconcile(tmp_path) == f"[OK] Host guard in sync with {sync.REPO}"
    service.ensure_running.assert_called_once()
    assert service.ensure_running.call_args.args[0]["AGENTIHOOKS_SWARM_REDIS_URL"] == "redis://proof:6379/0"


def test_installing_one_home_keeps_the_schedule_of_another(tmp_path, scheduler, calls):
    other = tmp_path / "other"
    other.mkdir()
    sync.install(other, enable=True)
    sync.install(tmp_path, enable=True)
    sync.install(tmp_path, enable=True)
    assert sorted(json.loads(record["payload"])["script"] for record in _records(scheduler)) == sorted(
        [str(other / "guard-daemon.sh"), str(tmp_path / "guard-daemon.sh")]
    )
    assert sync.drift(other) == []
    assert sync.drift(tmp_path) == []


def test_reconcile_reports_an_unreachable_scheduler_and_changes_nothing(tmp_path, unreachable, calls):
    (tmp_path / "guard-config.json").write_text("{}")
    assert sync.reconcile(tmp_path) == "[!!] Host guard not reconciled: the agentihooks scheduler is unreachable"
    assert not (tmp_path / "guard-config.json").is_symlink()


def test_an_unreachable_scheduler_is_reported_as_unchecked(tmp_path, unreachable):
    for name in LINKS:
        (tmp_path / name).symlink_to(sync.REPO / name)
    assert sync.drift(tmp_path) == ["the agentihooks scheduler is unreachable, so the guard schedule was not checked"]


def test_install_links_both_files_and_schedules_one_minute_interval(tmp_path, scheduler, calls):
    from scripts.scheduler import service

    assert sync.install(tmp_path, enable=True) == _installed(tmp_path)
    assert sync.install(tmp_path, enable=True) == _installed(tmp_path)
    for name in LINKS:
        assert (tmp_path / name).is_symlink()
        assert (tmp_path / name).readlink() == sync.REPO / name
    [record] = _records(scheduler)
    assert record["kind"] == "interval" and record["delay"] == 60 and record["state"] == "active"
    assert json.loads(record["payload"]) == {"script": str(tmp_path / "guard-daemon.sh")}
    assert [{key: slot[key] for key in ("signal", "kind", "reference")} for slot in record["slots"]] == [
        {"signal": "timeout", "kind": "callable", "reference": "scripts.host_guard.sync:tick"}
    ]
    assert service.ensure_running.call_count == 2
    started_with = service.ensure_running.call_args.args[0]
    assert started_with["AGENTIHOOKS_SWARM_REDIS_URL"] == os.environ["AGENTIHOOKS_SWARM_REDIS_URL"]
    assert sync.drift(tmp_path) == []
    assert calls == []


def test_install_replaces_a_stale_hand_copied_config(tmp_path, scheduler, calls):
    config = tmp_path / "guard-config.json"
    config.write_text('{"excludes": []}')
    sync.install(tmp_path, enable=True)
    assert config.is_symlink() and config.readlink() == sync.REPO / "guard-config.json"
    assert sync.drift(tmp_path) == []


def test_install_moves_a_live_systemd_guard_onto_the_scheduler(tmp_path, scheduler, monkeypatch, calls):
    monkeypatch.setattr(sync, "_live", lambda home: home == tmp_path)
    _legacy(tmp_path)
    assert sync.install(tmp_path, enable=True) == _installed(tmp_path)
    assert calls == [
        ("is-enabled", "--quiet", "guard-daemon.timer"),
        ("disable", "--now", "guard-daemon.timer"),
        ("daemon-reload",),
    ]
    assert not any((_units(tmp_path) / name).exists() for name in LEGACY)
    assert sync.drift(tmp_path) == []


def test_install_reports_an_unreachable_scheduler(tmp_path, unreachable, calls):
    message = f"[!!] Host guard linked in {tmp_path}; Redis is unreachable, so the guard is not scheduled"
    assert sync.install(tmp_path, enable=True) == message
    assert sync.run("install", tmp_path) == 1
    assert (tmp_path / "guard-config.json").readlink() == sync.REPO / "guard-config.json"


def test_systemctl_runs_the_user_manager_and_reads_its_exit(monkeypatch):
    seen = []

    def run(command, capture_output, timeout):
        seen.append((command, capture_output, timeout))
        return sync.subprocess.CompletedProcess(command, 0 if command[2] == "daemon-reload" else 3)

    monkeypatch.setattr(sync.subprocess, "run", run)
    assert sync._systemctl("daemon-reload") is True
    assert sync._systemctl("disable", "--now", "guard-daemon.timer") is False
    assert seen == [
        (["systemctl", "--user", "daemon-reload"], True, 20),
        (["systemctl", "--user", "disable", "--now", "guard-daemon.timer"], True, 20),
    ]


def test_systemctl_reads_a_missing_or_hung_manager_as_failure(monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError("systemctl")

    def hung(*args, **kwargs):
        raise sync.subprocess.TimeoutExpired("systemctl", 20)

    monkeypatch.setattr(sync.subprocess, "run", missing)
    assert sync._systemctl("daemon-reload") is False
    monkeypatch.setattr(sync.subprocess, "run", hung)
    assert sync._systemctl("daemon-reload") is False


def test_the_account_home_is_read_from_the_password_database(monkeypatch):
    seen = []
    monkeypatch.setattr(sync.os, "getuid", lambda: 4242)
    monkeypatch.setattr(sync.pwd, "getpwuid", lambda uid: seen.append(uid) or Mock(pw_dir="/home/proof"))
    assert sync._account_home() == Path("/home/proof")
    assert seen == [4242]


def test_the_scheduler_uses_the_swarm_redis(monkeypatch):
    from scripts.swarm import store

    client = Mock()
    monkeypatch.setattr(store, "redis_client", lambda: client)
    assert sync._scheduler().redis is client


def test_install_removes_a_lone_systemd_timer(tmp_path, scheduler, calls):
    _units(tmp_path).mkdir(parents=True)
    (_units(tmp_path) / "guard-daemon.timer").write_text("[Unit]\n")
    assert sync.install(tmp_path, enable=True) == _installed(tmp_path)
    assert not (_units(tmp_path) / "guard-daemon.timer").exists()


def test_reconcile_relinks_an_unscheduled_guard_without_scheduling_it(tmp_path, scheduler, calls):
    (tmp_path / "guard-config.json").write_text("{}")
    assert sync.reconcile(tmp_path) == (
        f"[--] Host guard linked in {tmp_path} from {sync.REPO}; agentihooks host-guard install schedules it"
    )
    assert _records(scheduler) == []
    assert (tmp_path / "guard-config.json").readlink() == sync.REPO / "guard-config.json"


def test_the_live_home_is_the_account_home(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "_account_home", lambda: tmp_path / "link")
    (tmp_path / "link").symlink_to(tmp_path)
    assert sync._live(tmp_path) is True
    assert sync._live(tmp_path / "other") is False


def test_reconcile_leaves_a_home_without_a_guard_alone(tmp_path, scheduler, calls):
    home = tmp_path / "bare"
    home.mkdir()
    assert sync.reconcile(home) == "[--] Host guard not installed; agentihooks host-guard install adds it"
    assert list(home.iterdir()) == []
    assert _records(scheduler) == []


def test_reconcile_moves_an_enabled_systemd_guard_onto_the_scheduler(tmp_path, scheduler, monkeypatch, calls):
    monkeypatch.setattr(sync, "_live", lambda home: True)
    _legacy(tmp_path)
    (tmp_path / "guard-config.json").write_text('{"excludes": []}')
    assert sync.reconcile(tmp_path) == _installed(tmp_path)
    assert sync.drift(tmp_path) == []


def test_reconcile_never_schedules_a_guard_the_operator_disabled(tmp_path, scheduler, monkeypatch):
    calls = []
    monkeypatch.setattr(sync, "_live", lambda home: True)
    monkeypatch.setattr(sync, "_systemctl", lambda *args: calls.append(args) or args[0] != "is-enabled")
    _legacy(tmp_path)
    (tmp_path / "guard-config.json").write_text("{}")
    assert sync.reconcile(tmp_path) == (
        f"[--] Host guard linked in {tmp_path} from {sync.REPO}; agentihooks host-guard install schedules it"
    )
    assert _records(scheduler) == []
    assert not any((_units(tmp_path) / name).exists() for name in LEGACY)
    assert (tmp_path / "guard-config.json").readlink() == sync.REPO / "guard-config.json"


def test_reconcile_relinks_a_scheduled_guard_and_keeps_one_schedule(tmp_path, scheduler, calls):
    sync.install(tmp_path, enable=True)
    config = tmp_path / "guard-config.json"
    config.unlink()
    config.write_text("{}")
    assert sync.reconcile(tmp_path) == _installed(tmp_path)
    assert len(_records(scheduler)) == 1
    assert sync.drift(tmp_path) == []


def test_reconcile_reports_a_guard_already_in_sync(tmp_path, scheduler, calls):
    sync.install(tmp_path, enable=True)
    assert sync.reconcile(tmp_path) == f"[OK] Host guard in sync with {sync.REPO}"
    assert len(_records(scheduler)) == 1


def _link(home: Path, *names: str) -> None:
    for name in names:
        (home / name).symlink_to(sync.REPO / name)


def test_reconcile_schedules_a_linked_guard_missing_from_the_current_scheduler(tmp_path, scheduler, calls):
    _link(tmp_path, *LINKS)
    assert sync.reconcile(tmp_path) == _installed(tmp_path)
    [record] = _records(scheduler)
    assert json.loads(record["payload"]) == {"script": str(tmp_path / "guard-daemon.sh")}
    assert sync.drift(tmp_path) == []


@pytest.mark.parametrize(
    ("linked", "copied"),
    [((), ("guard-config.json",)), (("guard-daemon.sh",), ("guard-config.json",)), (("guard-config.json",), ())],
)
def test_reconcile_leaves_a_home_without_both_links_unscheduled(tmp_path, scheduler, calls, linked, copied):
    _link(tmp_path, *linked)
    for name in copied:
        (tmp_path / name).write_text("{}")
    sync.reconcile(tmp_path)
    assert _records(scheduler) == []


def _stranded_entry(old, home: Path) -> None:
    entry = old.interval(owner=sync.OWNER, payload=json.dumps({"script": str(home / "guard-daemon.sh")}), every=60)
    entry.timeout.connect(sync.tick)
    entry.start()


def test_scheduling_retires_the_stranded_entry_of_the_same_home_only(tmp_path, stranded, scheduler, calls):
    other = tmp_path / "other"
    other.mkdir()
    _stranded_entry(stranded, tmp_path)
    _stranded_entry(stranded, other)
    _link(tmp_path, *LINKS)
    assert sync.reconcile(tmp_path) == _installed(tmp_path)
    assert [json.loads(record["payload"])["script"] for record in _records(stranded)] == [
        str(other / "guard-daemon.sh")
    ]
    assert len(_records(scheduler)) == 1


def test_an_unreachable_stranded_scheduler_still_schedules_the_guard(tmp_path, scheduler, monkeypatch, calls):
    from redis.exceptions import ConnectionError

    def refuse():
        raise ConnectionError("refused")

    monkeypatch.setattr(sync, "_stranded_scheduler", refuse)
    assert sync.install(tmp_path, enable=True) == (
        f"{_installed(tmp_path)}; a guard entry on the local Redis could not be checked or retired"
    )
    assert len(_records(scheduler)) == 1


def test_a_swarm_url_naming_the_local_redis_keeps_the_new_entry(tmp_path, scheduler, monkeypatch, calls):
    monkeypatch.setattr(sync, "_stranded_scheduler", lambda: scheduler)
    sync.install(tmp_path, enable=True)
    assert sync.install(tmp_path, enable=True) == _installed(tmp_path)
    [record] = _records(scheduler)
    assert json.loads(record["payload"]) == {"script": str(tmp_path / "guard-daemon.sh")}


def test_the_stranded_scheduler_is_the_local_redis_only_when_the_swarm_moved(monkeypatch):
    from scripts.swarm import store

    current = [store.DEFAULT_URL]
    monkeypatch.setattr(store, "redis_url", lambda environ: environ is os.environ and current[0])
    assert sync._stranded_scheduler() is None
    current[0] = "redis://remote:6379/0"
    options = sync._stranded_scheduler().redis.connection_pool.connection_kwargs
    assert (options["host"], options["port"], options["db"]) == ("127.0.0.1", 6379, 0)
    assert options["decode_responses"] is True
    assert (options["socket_connect_timeout"], options["socket_timeout"]) == (3, 10)


def test_check_exits_one_and_names_the_drift_then_zero_after_install(tmp_path, scheduler, capsys, calls):
    (tmp_path / "guard-config.json").write_text("{}")
    assert sync.run("check", tmp_path) == 1
    assert capsys.readouterr().out.splitlines() == [
        f"{tmp_path / 'guard-daemon.sh'} is missing",
        f"{tmp_path / 'guard-config.json'} is not a link to {sync.REPO / 'guard-config.json'}",
        f"{tmp_path / 'guard-daemon.sh'} is not scheduled every 60s on the agentihooks scheduler",
    ]
    assert sync.run("install", tmp_path) == 0
    assert capsys.readouterr().out == f"{_installed(tmp_path)}\n"
    assert sync.run("check", tmp_path) == 0
    assert capsys.readouterr().out == f"[OK] Host guard in sync with {sync.REPO}\n"


def test_tick_runs_the_home_script_once_per_delivery(monkeypatch):
    import fakeredis

    started = []
    monkeypatch.setattr(sync.shutil, "which", lambda name: "/usr/bin/timeout" if name == "timeout" else None)
    monkeypatch.setattr(sync.subprocess, "Popen", lambda command, **options: started.append((command, options)))
    redis = fakeredis.FakeRedis(decode_responses=True)
    event = {"payload": json.dumps({"script": "/home/me/guard-daemon.sh"})}
    sync.tick(event, "delivery", redis)
    sync.tick(event, "delivery", redis)
    devnull = sync.subprocess.DEVNULL
    assert started == [
        (
            ["timeout", "120", "/home/me/guard-daemon.sh"],
            {"stdin": devnull, "stdout": devnull, "stderr": devnull, "start_new_session": True},
        )
    ]
    from hooks._redis import redis_key

    key = redis_key("scheduler", "agentihooks-host-guard:delivery")
    assert redis.keys() == [key]
    assert redis.get(key) == "1"
    assert 0 < redis.ttl(key) <= 3600


def test_tick_runs_without_timeout_and_logs_a_failed_start(monkeypatch):
    import fakeredis

    from hooks import common

    started, logged = [], []
    monkeypatch.setattr(sync.shutil, "which", lambda name: None)

    def refuse(command, **options):
        started.append(command)
        raise PermissionError("denied")

    monkeypatch.setattr(sync.subprocess, "Popen", refuse)
    monkeypatch.setattr(common, "log", lambda message, data: logged.append((message, data)))
    sync.tick({"payload": json.dumps({"script": "/g.sh"})}, "one", fakeredis.FakeRedis(decode_responses=True))
    assert started == [["/g.sh"]]
    assert logged == [("host guard failed to start", {"error": "denied"})]


def test_the_gc_command_parses_host_guard_actions():
    parser = gc_cli._parser()
    assert f" host-guard {HELP} options:" in " ".join(parser.format_help().split())
    for action in ("install", "check"):
        args = parser.parse_args(["host-guard", action])
        assert (args.command, args.action) == ("host-guard", action)
    with pytest.raises(SystemExit):
        parser.parse_args(["host-guard", "remove"])


def test_the_gc_command_runs_host_guard_in_the_home(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(gc_cli.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(sync, "run", lambda action, home: seen.append((action, home)) or 3)
    assert gc_cli.main(["host-guard", "check"]) == 3
    assert gc_cli.main(["host-guard", "install"]) == 3
    assert seen == [("check", tmp_path), ("install", tmp_path)]


def test_the_agentihooks_command_dispatches_host_guard(tmp_path, monkeypatch):
    from scripts import install as cli

    seen = []
    monkeypatch.setattr(sync.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(sync, "run", lambda action, home: seen.append((action, home)) or 3)
    for action in ("check", "install"):
        monkeypatch.setattr(cli.sys, "argv", ["agentihooks", "host-guard", action])
        with pytest.raises(SystemExit) as raised:
            cli.main()
        assert raised.value.code == 3
    assert seen == [("check", tmp_path), ("install", tmp_path)]


@pytest.mark.parametrize(("enabled", "schedule"), [(True, "installed"), (False, "removed")])
def test_init_reconciles_the_host_guard_beside_the_lifecycle_sweep(tmp_path, monkeypatch, enabled, schedule):
    from hooks.lifecycle import timer

    seen = []
    monkeypatch.setattr(timer.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(timer, "install_timer", lambda python, root: seen.append((python, root)) or "installed")
    monkeypatch.setattr(timer, "remove_timer", lambda: "removed")
    monkeypatch.setattr(sync, "reconcile", lambda home: f"guard {home}")
    assert timer.reconcile_units("/venv/bin/python", "/opt/agentihooks", enabled) == f"{schedule}\n  guard {tmp_path}"
    assert seen == ([("/venv/bin/python", "/opt/agentihooks")] if enabled else [])
