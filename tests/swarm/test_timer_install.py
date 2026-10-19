from argparse import Namespace

import pytest

from scripts.swarm import cli, timer
from scripts.swarm.store import SwarmError

real_roots = timer._roots
real_reconcile = timer.reconcile
real_schedule = timer.schedule


@pytest.fixture
def installed(tmp_path):
    root = tmp_path / "agentihooks"
    bin_dir = tmp_path / "tool" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "agentihooks").write_text("#!/bin/sh\n")
    return root, bin_dir


def test_the_entry_point_ignores_a_scratch_shim_first_on_path(installed, tmp_path):
    _, bin_dir = installed
    shim = tmp_path / "scratch" / "agentihooks"
    shim.parent.mkdir()
    shim.write_text("#!/bin/sh\n")
    assert timer.entry_point(bin_dir, which=lambda name: str(shim)) == str(bin_dir / "agentihooks")


def test_the_entry_point_keeps_a_path_link_to_the_installed_script(installed, tmp_path):
    _, bin_dir = installed
    link = tmp_path / "local" / "agentihooks"
    link.parent.mkdir()
    link.symlink_to(bin_dir / "agentihooks")
    found = {"agentihooks": str(link)}
    assert timer.entry_point(bin_dir, which=found.get) == str(link)


def test_a_run_from_a_worktree_is_refused_and_names_both_roots(monkeypatch, tmp_path):
    worktree, primary = tmp_path / "worktree", tmp_path / "primary"
    monkeypatch.setattr(timer, "_roots", lambda: (worktree, primary))
    why = timer.installed_refusal()
    assert str(worktree) in why and str(primary) in why and "installed agentihooks" in why


def test_a_run_from_the_installed_root_is_not_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(timer, "_roots", lambda: (tmp_path, tmp_path))
    assert timer.installed_refusal() == ""


def test_the_roots_are_the_running_checkout_and_the_install_root(monkeypatch, tmp_path):
    from scripts.targets._common import _install_module

    install = _install_module()
    monkeypatch.setattr(install, "AGENTIHOOKS_ROOT", tmp_path / "worktree")
    monkeypatch.setattr(install, "install_root", lambda: tmp_path / "primary")
    assert real_roots() == (tmp_path / "worktree", tmp_path / "primary")


class _Store:
    state = "paused"
    redis = Namespace(delete=lambda key: None)

    def key(self, slug, name):
        return name

    def update(self, slug, state):
        self.state = state

    def config(self, slug):
        return Namespace(state=self.state)


def test_starting_writes_the_unit_with_the_installed_entry_point(monkeypatch, installed, capsys):
    _, bin_dir = installed
    written = []
    monkeypatch.setattr(timer, "entry_point", lambda: str(bin_dir / "agentihooks"))
    monkeypatch.setattr(timer, "ensure", lambda binary: written.append(binary) or True)
    monkeypatch.setattr(cli, "run_tick", lambda store, slug: [])
    cli._state(_Store(), Namespace(slug="sw"), "running")
    assert written == [str(bin_dir / "agentihooks")]
    assert "warning" not in capsys.readouterr().err


def test_starting_warns_when_the_timer_cannot_be_enabled(monkeypatch, capsys):
    monkeypatch.setattr(timer, "entry_point", lambda: "/installed/bin/agentihooks")
    monkeypatch.setattr(timer, "ensure", lambda binary: False)
    monkeypatch.setattr(cli, "run_tick", lambda store, slug: [])
    cli._state(_Store(), Namespace(slug="sw"), "running")
    assert (
        "warning: the scheduled swarm tick or swarm services could not be enabled; run agentihooks swarm tick yourself\n"
        in capsys.readouterr().err
    )


def test_starting_from_a_non_installed_run_says_why_and_writes_nothing(monkeypatch, capsys, tmp_path):
    from scripts.targets._common import _install_module

    install = _install_module()
    monkeypatch.setattr(install, "AGENTIHOOKS_ROOT", tmp_path / "worktree")
    monkeypatch.setattr(install, "install_root", lambda: tmp_path / "primary")
    monkeypatch.setattr(timer, "UNIT_DIR", tmp_path / "units")
    monkeypatch.setattr(cli, "run_tick", lambda store, slug: [])
    cli._state(_Store(), Namespace(slug="sw"), "running")
    err = capsys.readouterr().err
    assert f"this run comes from {tmp_path / 'worktree'}" in err and "timer" in err
    assert not (tmp_path / "units").exists()


def test_the_tick_refuses_a_non_installed_run_before_touching_any_swarm(monkeypatch):
    monkeypatch.setattr(timer, "installed_refusal", lambda: "this run comes from a worktree")

    class Store:
        def slugs(self):
            pytest.fail("the tick ran")

    with pytest.raises(SwarmError, match="this run comes from a worktree"):
        cli.cmd_tick(Store(), Namespace())


def test_a_refused_tick_exits_non_zero_and_logs_why(monkeypatch, capsys):
    monkeypatch.setattr(timer, "installed_refusal", lambda: "this run comes from a worktree")
    monkeypatch.setattr(cli, "connect", lambda: None)
    assert cli.main(["tick"]) == 1
    assert "this run comes from a worktree" in capsys.readouterr().err


def test_wait_checks_have_a_separate_short_timer_and_service(tmp_path):
    import subprocess
    from configparser import ConfigParser

    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    def installed(name):
        parsed = ConfigParser(interpolation=None)
        parsed.optionxform = str
        parsed.read_string((tmp_path / name).read_text())
        return parsed

    assert timer.ensure("/installed/bin/agentihooks", tmp_path, run=run)
    wait_timer = installed("agentihooks-swarm-waits.timer")
    wait_service = installed("agentihooks-swarm-waits.service")
    assert wait_timer.sections() == ["Unit", "Timer", "Install"]
    assert wait_service.sections() == ["Unit", "Service"]
    assert wait_timer["Unit"]["Description"]
    assert wait_service["Unit"]["Description"]
    assert dict(wait_timer["Timer"]) == {"OnBootSec": "15s", "OnUnitActiveSec": "15s", "AccuracySec": "1s"}
    assert dict(wait_timer["Install"]) == {"WantedBy": "timers.target"}
    assert dict(wait_service["Service"]) == {
        "Type": "oneshot",
        "Environment": "PATH=%h/.local/bin:%h/.cargo/bin:/usr/local/bin:/usr/bin:/bin",
        "EnvironmentFile": "-%h/.agentihooks/.env",
        "ExecStart": '"/installed/bin/agentihooks" swarm waits',
        "TimeoutStartSec": "120",
    }
    assert ["systemctl", "--user", "enable", "--now", "agentihooks-swarm-waits.timer"] in calls
    assert ["systemctl", "--user", "enable", "--now", "agentihooks-swarm-waits.service"] not in calls


def _systemctl(calls, failing=()):
    import subprocess

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 1 if argv[2] in failing else 0)

    return run


def test_a_tick_timer_that_cannot_be_disabled_keeps_its_files_for_a_later_pass(tmp_path, capsys):
    assert timer.ensure("/installed/bin/agentihooks", tmp_path, run=_systemctl([]))
    capsys.readouterr()
    for name in ("agentihooks-swarm.timer", "agentihooks-swarm.service"):
        (tmp_path / name).write_text("[Unit]\n")
    calls = []
    timer.ensure("/installed/bin/agentihooks", tmp_path, run=_systemctl(calls, failing={"disable"}))
    assert ["systemctl", "--user", "daemon-reload"] not in [argv for argv, _ in calls]
    assert (tmp_path / "agentihooks-swarm.timer").exists()
    assert (tmp_path / "agentihooks-swarm.service").exists()
    assert capsys.readouterr().err == (
        "the agentihooks-swarm timer could not be disabled; its unit files stay until a later pass\n"
    )


def test_an_upgrade_disables_and_deletes_the_tick_timer_without_stopping_a_running_tick(tmp_path, monkeypatch):
    scheduled = []
    monkeypatch.setattr(timer, "schedule", lambda: scheduled.append(True) or True)
    (tmp_path / "agentihooks-swarm.timer").write_text("[Timer]\nOnUnitActiveSec=60s\n")
    (tmp_path / "agentihooks-swarm.service").write_text("[Service]\nType=oneshot\n")
    calls = []
    assert timer.ensure("/installed/bin/agentihooks", tmp_path, run=_systemctl(calls))
    assert not (tmp_path / "agentihooks-swarm.timer").exists()
    assert not (tmp_path / "agentihooks-swarm.service").exists()
    argvs = [argv for argv, _ in calls]
    assert argvs[0] == ["systemctl", "--user", "disable", "--now", "agentihooks-swarm.timer"]
    assert calls[0][1] == {"capture_output": True, "text": True, "timeout": 30, "check": False}
    assert argvs[1] == ["systemctl", "--user", "daemon-reload"]
    assert not [argv for argv in argvs if "agentihooks-swarm.service" in argv]
    assert not [argv for argv in argvs if "stop" in argv]
    assert scheduled == [True]


def test_removing_one_leftover_tick_unit_still_reloads_systemd(tmp_path):
    calls = []
    assert timer.ensure("/installed/bin/agentihooks", tmp_path, run=_systemctl(calls))
    (tmp_path / "agentihooks-swarm.service").write_text("[Service]\nType=oneshot\n")
    calls.clear()
    assert timer.ensure("/installed/bin/agentihooks", tmp_path, run=_systemctl(calls))
    assert not (tmp_path / "agentihooks-swarm.service").exists()
    assert [argv for argv, _ in calls][:2] == [
        ["systemctl", "--user", "disable", "--now", "agentihooks-swarm.timer"],
        ["systemctl", "--user", "daemon-reload"],
    ]
    calls.clear()
    assert timer.ensure("/installed/bin/agentihooks", tmp_path, run=_systemctl(calls))
    assert [argv[2] for argv, _ in calls] == ["enable"] * 3


def test_ensure_fails_when_the_scheduled_tick_cannot_be_registered(tmp_path, monkeypatch):
    monkeypatch.setattr(timer, "schedule", lambda: False)
    calls = []
    assert timer.ensure("/installed/bin/agentihooks", tmp_path, run=_systemctl(calls)) is False
    assert [argv[2] for argv, _ in calls][-3:] == ["enable"] * 3


def test_reconcile_reruns_ensure_while_a_retired_tick_unit_is_left(tmp_path, monkeypatch):
    monkeypatch.setattr(timer, "reconcile", real_reconcile)
    calls = []
    assert timer.ensure("/installed/bin/agentihooks", tmp_path, run=_systemctl(calls))
    for name in ("agentihooks-swarm.timer", "agentihooks-swarm.service"):
        (tmp_path / name).write_text("[Unit]\n")
        calls.clear()
        assert timer.reconcile("/installed/bin/agentihooks", tmp_path, run=_systemctl(calls))
        assert calls[0][0] == ["systemctl", "--user", "disable", "--now", "agentihooks-swarm.timer"]
        assert not (tmp_path / name).exists()


def test_reconcile_of_current_units_returns_whether_the_tick_is_scheduled(tmp_path, monkeypatch):
    monkeypatch.setattr(timer, "reconcile", real_reconcile)
    calls = []
    assert timer.ensure("/installed/bin/agentihooks", tmp_path, run=_systemctl(calls))
    for outcome in (True, False):
        monkeypatch.setattr(timer, "schedule", lambda outcome=outcome: outcome)
        calls.clear()
        assert timer.reconcile("/installed/bin/agentihooks", tmp_path, run=_systemctl(calls)) is outcome
        assert {argv[2] for argv, _ in calls} == {"is-enabled"}


def test_schedule_registers_the_tick_and_starts_the_scheduler_runner(monkeypatch):
    from scripts.scheduler import service
    from scripts.swarm import scheduled_tick, store

    seen = []
    monkeypatch.delenv("AGENTIHOOKS_SWARM_REDIS_URL", raising=False)
    monkeypatch.delenv("AGENTIHOOKS_DEPLOYMENT", raising=False)
    monkeypatch.setenv("SCHEDULE_MARKER", "kept")
    monkeypatch.setattr(store, "redis_client", lambda: "redis")
    monkeypatch.setattr(scheduled_tick, "ensure", lambda scheduler: seen.append(("tick", scheduler.redis)))
    monkeypatch.setattr(
        service,
        "ensure_running",
        lambda environ: seen.append(("runner", environ["AGENTIHOOKS_SWARM_REDIS_URL"], environ["SCHEDULE_MARKER"])),
    )
    assert real_schedule() is True
    assert seen == [("tick", "redis"), ("runner", "redis://127.0.0.1:6379/0", "kept")]

    seen.clear()
    monkeypatch.setenv("AGENTIHOOKS_SWARM_REDIS_URL", "redis://swarm:6379/3")
    assert real_schedule() is True
    assert seen[1] == ("runner", "redis://swarm:6379/3", "kept")


def test_schedule_reports_a_redis_failure_and_starts_no_runner(monkeypatch, capsys):
    from redis.exceptions import ConnectionError

    from scripts.scheduler import service
    from scripts.swarm import scheduled_tick, store

    def refuse(scheduler):
        raise ConnectionError("down")

    started = []
    monkeypatch.setattr(store, "redis_client", lambda: "redis")
    monkeypatch.setattr(scheduled_tick, "ensure", refuse)
    monkeypatch.setattr(service, "ensure_running", lambda environ: started.append(True))
    assert real_schedule() is False
    assert started == []
    assert capsys.readouterr().err == "the scheduled swarm tick could not be registered: ConnectionError: down\n"
