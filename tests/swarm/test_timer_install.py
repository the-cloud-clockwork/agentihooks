import os
import subprocess
from argparse import Namespace

import pytest

from scripts.swarm import cli, timer
from scripts.swarm.store import SwarmError

real_roots = timer._roots
real_reconcile = timer.reconcile
real_schedule = timer.schedule
real_undelivered = timer.undelivered
real_guard = timer.guard


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


def test_a_run_from_a_linked_git_worktree_leaves_the_shared_units_alone(monkeypatch, tmp_path, capsys):
    from scripts.targets._common import _install_module

    install = _install_module()
    monkeypatch.setattr(timer, "reconcile", real_reconcile)
    worktree = tmp_path / "worktrees" / "agentihooks" / "engineer-1"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: /home/dev/agentihooks/.git/worktrees/engineer-1\n")
    monkeypatch.setattr(install, "AGENTIHOOKS_ROOT", worktree)
    monkeypatch.setattr(install, "install_root", lambda: worktree)
    shared = tmp_path / "systemd" / "user"
    monkeypatch.setattr(timer, "UNIT_DIR", shared)
    shared.mkdir(parents=True)
    (shared / "agentihooks-inbox-waker.service").write_text('ExecStart="/installed/bin/agentihooks" swarm waker\n')
    (shared / "agentihooks-swarm.timer").write_text("[Timer]\n")
    before = {path.name: path.read_text() for path in shared.iterdir()}
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    assert timer.ensure("/worktree/bin/agentihooks", run=run) is False
    assert timer.reconcile("/worktree/bin/agentihooks", run=run) is False
    assert {path.name: path.read_text() for path in shared.iterdir()} == before

    (shared / "agentihooks-swarm.timer").unlink()
    for name, text in timer.units("/worktree/bin/agentihooks").items():
        (shared / name).write_text(text)
    current = {path.name: path.read_text() for path in shared.iterdir()}
    assert timer.reconcile("/worktree/bin/agentihooks", run=run) is False
    assert {path.name: path.read_text() for path in shared.iterdir()} == current

    assert calls == []
    refusal = (
        f"this run comes from the git worktree {worktree}, "
        f"so it leaves the shared swarm timer units in {shared} alone\n"
    )
    assert capsys.readouterr().err == refusal * 3

    proof_dir = tmp_path / "proof-units"
    assert timer.ensure("/worktree/bin/agentihooks", proof_dir, run=run)


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


def test_the_stale_tick_watcher_is_its_own_restarting_user_service():
    from configparser import ConfigParser

    parsed = ConfigParser(interpolation=None, strict=False)
    parsed.optionxform = str
    parsed.read_string(timer.units("/installed/bin/agentihooks")["agentihooks-tick-watch.service"])

    assert parsed.sections() == ["Unit", "Service", "Install"]
    assert parsed["Unit"]["Description"] == "agentihooks stale tick watcher, apart from the tick it watches"
    assert dict(parsed["Service"]) == {
        "Type": "simple",
        "Environment": "PATH=%h/.local/bin:%h/.cargo/bin:/usr/local/bin:/usr/bin:/bin",
        "EnvironmentFile": "-%h/.agentihooks/.env",
        "ExecStart": '"/installed/bin/agentihooks" swarm tick-watch',
        "Restart": "always",
        "RestartSec": "5",
        "NotifyAccess": "main",
        "WatchdogSec": "300",
    }
    assert dict(parsed["Install"]) == {"WantedBy": "default.target"}


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
        "KillMode": "process",
    }
    assert ["systemctl", "--user", "enable", "--now", "agentihooks-swarm-waits.timer"] in calls
    assert ["systemctl", "--user", "enable", "--now", "agentihooks-swarm-waits.service"] not in calls


def test_a_wait_service_without_kill_mode_process_is_rewritten_and_reloaded(tmp_path, monkeypatch):
    monkeypatch.setattr(timer, "reconcile", real_reconcile)
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    assert timer.ensure("/installed/bin/agentihooks", tmp_path, run=run)
    service = tmp_path / "agentihooks-swarm-waits.service"
    service.write_text(service.read_text().replace("KillMode=process\n", ""))
    calls.clear()
    assert timer.reconcile("/installed/bin/agentihooks", tmp_path, run=run)
    assert service.read_text().endswith("TimeoutStartSec=120\nKillMode=process\n")
    assert calls[0] == ["systemctl", "--user", "daemon-reload"]


def _systemctl(calls, failing=()):
    import subprocess

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 1 if argv[2] in failing else 0, "", "")

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


def _draining(calls, groups, clock, shown=0):
    import subprocess

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        clock.append(clock[-1] + 1)
        if argv[2] == "show":
            group, tasks = groups.pop(0) if len(groups) > 1 else groups[0]
            return subprocess.CompletedProcess(argv, shown, f"ControlGroup={group}\nTasksCurrent={tasks}\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    return run


SHOW = ["systemctl", "--user", "show", "-p", "ControlGroup", "-p", "TasksCurrent", "agentihooks-swarm.service"]
KILL = ["systemctl", "--user", "kill", "--kill-who=all"]
STOP = ["systemctl", "--user", "stop", "agentihooks-swarm.service"]


def test_an_upgrade_ends_what_the_tick_service_left_and_waits_for_its_group_before_scheduling(tmp_path, monkeypatch):
    calls, clock = [], [0.0]
    monkeypatch.setattr(timer, "schedule", lambda: calls.append((["schedule"], {})) or True)
    monkeypatch.setattr(timer.time, "monotonic", lambda: clock[-1])
    monkeypatch.setattr(timer.time, "sleep", lambda seconds: None)
    (tmp_path / "agentihooks-swarm.timer").write_text("[Timer]\nOnUnitActiveSec=60s\n")
    (tmp_path / "agentihooks-swarm.service").write_text("[Service]\nType=oneshot\nKillMode=process\n")
    group = "/user.slice/agentihooks-swarm.service"
    groups = [(group, "3"), (group, "1"), ("", "[not set]")]
    assert timer.ensure("/installed/bin/agentihooks", tmp_path, run=_draining(calls, groups, clock))
    assert not (tmp_path / "agentihooks-swarm.timer").exists()
    assert not (tmp_path / "agentihooks-swarm.service").exists()
    argvs = [argv for argv, _ in calls]
    assert argvs[:7] == [
        ["systemctl", "--user", "disable", "--now", "agentihooks-swarm.timer"],
        STOP,
        SHOW,
        [*KILL, "--signal=SIGTERM", "agentihooks-swarm.service"],
        SHOW,
        SHOW,
        ["systemctl", "--user", "daemon-reload"],
    ]
    assert calls[0][1] == {"capture_output": True, "text": True, "timeout": 30, "check": False}
    assert calls[1][1] == {"capture_output": True, "text": True, "timeout": 120, "check": False}
    assert argvs[-1] == ["schedule"]


@pytest.mark.parametrize(("shown", "left"), [(0, "2"), (1, "unknown")])
def test_a_tick_service_group_that_will_not_empty_is_killed_and_keeps_its_units(
    tmp_path, monkeypatch, capsys, shown, left
):
    calls, clock, scheduled, slept = [], [0.0], [], []
    monkeypatch.setattr(timer.time, "monotonic", lambda: clock[-1])
    monkeypatch.setattr(timer.time, "sleep", slept.append)
    assert timer.ensure("/installed/bin/agentihooks", tmp_path, run=_systemctl([]))
    monkeypatch.setattr(timer, "schedule", lambda: scheduled.append(True) or True)
    (tmp_path / "agentihooks-swarm.timer").write_text("[Timer]\n")
    (tmp_path / "agentihooks-swarm.service").write_text("[Service]\n")
    groups = [("/user.slice/agentihooks-swarm.service", "2")]
    timer.ensure("/installed/bin/agentihooks", tmp_path, run=_draining(calls, groups, clock, shown))
    argvs = [argv for argv, _ in calls]
    kills = [argv for argv in argvs if argv[:4] == KILL]
    assert kills == [
        [*KILL, "--signal=SIGTERM", "agentihooks-swarm.service"],
        [*KILL, "--signal=SIGKILL", "agentihooks-swarm.service"],
    ]
    assert (tmp_path / "agentihooks-swarm.timer").exists()
    assert (tmp_path / "agentihooks-swarm.service").exists()
    assert ["systemctl", "--user", "daemon-reload"] not in argvs
    assert capsys.readouterr().err == (
        f"the agentihooks-swarm service group did not empty ({left} processes left); "
        "its unit files stay until a later pass\n"
    )
    assert clock[-1] >= timer.DRAIN_S + timer.KILL_WAIT_S
    assert scheduled == [True]
    assert slept and set(slept) == {1}
    plain = {"capture_output": True, "text": True, "timeout": 30, "check": False}
    assert {argv[2]: kwargs for argv, kwargs in calls if argv[2] in ("show", "kill")} == {"show": plain, "kill": plain}


@pytest.mark.parametrize(
    ("stdout", "left"),
    [
        ("ControlGroup=/x\n", "unknown"),
        ("ControlGroup=/a=b\nTasksCurrent=3\n", "3"),
        ("ControlGroup=\nTasksCurrent=[not set]\n", "0"),
        ("", "0"),
    ],
)
def test_the_group_count_reads_the_shown_properties(stdout, left):
    import subprocess

    shown = lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, stdout, "")  # noqa: E731
    assert timer._left(shown) == left


def test_a_group_that_empties_at_the_last_look_is_drained(monkeypatch):
    import itertools

    seen = iter(["2", "2", "0"])
    monkeypatch.setattr(timer, "_left", lambda run: next(seen))
    monkeypatch.setattr(timer.time, "monotonic", itertools.cycle([0.0, 1e9]).__next__)
    assert timer._drain(_systemctl([])) == ""


def _no_sleep(seconds):
    raise AssertionError(f"slept {seconds} after the deadline")


def test_a_drain_with_no_time_left_looks_once_more_and_never_sleeps(monkeypatch):
    seen = []
    monkeypatch.setattr(timer, "_left", lambda run: seen.append(True) or "2")
    monkeypatch.setattr(timer, "DRAIN_S", 0)
    monkeypatch.setattr(timer, "KILL_WAIT_S", 0)
    monkeypatch.setattr(timer.time, "monotonic", lambda: 5.0)
    monkeypatch.setattr(timer.time, "sleep", _no_sleep)
    assert timer._drain(_systemctl([])) == "2"
    assert len(seen) == 3


def test_an_inside_drain_with_no_time_left_never_sleeps(tmp_path, monkeypatch):
    monkeypatch.setattr(timer, "_others", lambda procs: {4242})
    monkeypatch.setattr(timer, "DRAIN_S", 0)
    monkeypatch.setattr(timer, "KILL_WAIT_S", 0)
    monkeypatch.setattr(timer.os, "kill", lambda pid, number: None)
    monkeypatch.setattr(timer.time, "monotonic", lambda: 5.0)
    monkeypatch.setattr(timer.time, "sleep", _no_sleep)
    assert timer._drain_inside(tmp_path / "cgroup.procs") == "1"


def test_an_inside_drain_polls_each_second_until_each_deadline(tmp_path, monkeypatch):
    import signal

    clock, slept, sent = [0.0], [], []

    def sleep(seconds):
        if len(slept) > 20:
            raise AssertionError("the drain kept polling past its deadlines")
        slept.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(timer, "_others", lambda procs: {4242})
    monkeypatch.setattr(timer, "DRAIN_S", 3)
    monkeypatch.setattr(timer, "KILL_WAIT_S", 2)
    monkeypatch.setattr(timer.os, "kill", lambda pid, number: sent.append((pid, number)))
    monkeypatch.setattr(timer.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(timer.time, "sleep", sleep)
    assert timer._drain_inside(tmp_path / "cgroup.procs") == "1"
    assert slept == [1] * 5
    assert sent == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]


def test_a_tick_running_inside_the_tick_service_ends_the_rest_of_its_group_but_not_itself(tmp_path, monkeypatch):
    import signal
    import subprocess
    import sys

    from scripts.swarm import tick_reaper

    monkeypatch.setattr(tick_reaper, "PROC", timer.Path("/proc"))
    left = [subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"]) for _ in range(2)]
    procs = tmp_path / "cgroup.procs"
    procs.write_text("".join(f"{pid}\n" for pid in [os.getpid(), *[p.pid for p in left]]))
    monkeypatch.setattr(timer, "_own_group", lambda: procs)
    (tmp_path / "agentihooks-swarm.timer").write_text("[Timer]\n")
    (tmp_path / "agentihooks-swarm.service").write_text("[Service]\n")
    calls = []
    try:
        assert timer.ensure("/installed/bin/agentihooks", tmp_path, run=_systemctl(calls))
        assert [p.wait(15) for p in left] == [-signal.SIGTERM] * 2
    finally:
        for p in left:
            p.kill()
            p.wait()
    argvs = [argv for argv, _ in calls]
    assert argvs[:2] == [
        ["systemctl", "--user", "disable", "--now", "agentihooks-swarm.timer"],
        ["systemctl", "--user", "daemon-reload"],
    ]
    assert not [argv for argv in argvs if argv[2] in ("stop", "kill", "show")]
    assert not (tmp_path / "agentihooks-swarm.timer").exists()
    assert not (tmp_path / "agentihooks-swarm.service").exists()


@pytest.mark.wall_clock
def test_a_group_member_that_ignores_terminate_is_killed_and_an_undrained_group_keeps_the_units(
    tmp_path, monkeypatch, capsys
):
    import signal
    import subprocess
    import sys

    deaf = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); time.sleep(600)"
    from scripts.swarm import tick_reaper

    monkeypatch.setattr(tick_reaper, "PROC", timer.Path("/proc"))
    left = subprocess.Popen([sys.executable, "-c", deaf], stdout=subprocess.PIPE, text=True)
    procs = tmp_path / "cgroup.procs"
    procs.write_text(f"{os.getpid()}\n{left.pid}\n")
    monkeypatch.setattr(timer, "DRAIN_S", 1)
    slept, real_sleep = [], timer.time.sleep
    monkeypatch.setattr(timer.time, "sleep", lambda seconds: slept.append(seconds) or real_sleep(0.05))
    try:
        assert left.stdout.readline().strip() == "ready"
        started = timer.time.monotonic()
        assert timer._drain_inside(procs) == ""
        assert timer.time.monotonic() - started >= 1
        assert slept and set(slept) == {1}
        assert left.wait(15) == -signal.SIGKILL
    finally:
        left.kill()
        left.wait()
    monkeypatch.setattr(timer, "_others", lambda procs: {4242})
    monkeypatch.setattr(timer, "KILL_WAIT_S", 0)
    monkeypatch.setattr(timer.os, "kill", lambda pid, number: None)
    assert timer._drain_inside(procs) == "1"


def test_a_member_gone_before_its_signal_is_skipped(tmp_path, monkeypatch):
    import subprocess
    import sys

    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    members = iter([{gone.pid}, set()])
    monkeypatch.setattr(timer, "_others", lambda procs: next(members))
    assert timer._drain_inside(tmp_path / "cgroup.procs") == ""


def test_an_unreadable_own_group_keeps_the_units(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(timer, "_own_group", lambda: tmp_path / "gone" / "cgroup.procs")
    (tmp_path / "agentihooks-swarm.timer").write_text("[Timer]\n")
    (tmp_path / "agentihooks-swarm.service").write_text("[Service]\n")
    timer.ensure("/installed/bin/agentihooks", tmp_path, run=_systemctl([]))
    assert (tmp_path / "agentihooks-swarm.timer").exists()
    assert (tmp_path / "agentihooks-swarm.service").exists()
    assert "the agentihooks-swarm service group did not empty (unknown processes left)" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("lines", "procs"),
    [
        ("0::/user.slice/user@1000.service/app.slice/agentihooks-swarm.service\n", "user.slice"),
        (
            "12:cpu,cpuacct:/user.slice\n1:name=systemd:/user.slice/user@1000.service/agentihooks-swarm.service\n",
            "systemd/user.slice",
        ),
        ("0::/user.slice/a:b/agentihooks-swarm.service\n", "user.slice/a:b"),
        ("0::/Xa.slice/agentihooks-swarm.service\n", "Xa.slice"),
        ("0::/user.slice/user@1000.service/app.slice/agentihooks-swarm-waits.service\n", None),
        (None, None),
    ],
)
def test_the_own_group_is_the_tick_service_cgroup_on_either_hierarchy(tmp_path, monkeypatch, lines, procs):
    cgroup = tmp_path / "cgroup"
    if lines is not None:
        cgroup.write_text(lines)
    monkeypatch.setattr(timer, "CGROUP_FILE", cgroup)
    monkeypatch.setattr(timer, "CGROUP_ROOT", tmp_path / "fs")
    found = timer._own_group()
    if procs is None:
        assert found is None
    else:
        assert found.name == "cgroup.procs"
        assert found.parent.name == "agentihooks-swarm.service"
        assert str(found).startswith(str(tmp_path / "fs" / procs))


def test_removing_one_leftover_tick_unit_still_reloads_systemd(tmp_path):
    calls = []
    assert timer.ensure("/installed/bin/agentihooks", tmp_path, run=_systemctl(calls))
    (tmp_path / "agentihooks-swarm.service").write_text("[Service]\nType=oneshot\n")
    calls.clear()
    assert timer.ensure("/installed/bin/agentihooks", tmp_path, run=_systemctl(calls))
    assert not (tmp_path / "agentihooks-swarm.service").exists()
    assert [argv for argv, _ in calls][:4] == [
        ["systemctl", "--user", "disable", "--now", "agentihooks-swarm.timer"],
        STOP,
        SHOW,
        ["systemctl", "--user", "daemon-reload"],
    ]
    calls.clear()
    assert timer.ensure("/installed/bin/agentihooks", tmp_path, run=_systemctl(calls))
    assert [argv[2] for argv, _ in calls] == ["enable"] * 4


def test_ensure_fails_and_keeps_the_tick_timer_when_registration_fails_before_any_delivery(tmp_path, monkeypatch):
    monkeypatch.setattr(timer, "schedule", lambda: False)
    monkeypatch.setattr(timer, "undelivered", lambda: NONE_YET)
    calls = []
    assert timer.ensure("/installed/bin/agentihooks", tmp_path, run=_systemctl(calls)) is False
    assert [argv[2] for argv, _ in calls][-4:] == ["enable"] * 4
    assert calls[-1][0] == ["systemctl", "--user", "enable", "--now", "agentihooks-swarm.timer"]
    assert (tmp_path / "agentihooks-swarm.timer").read_text() == timer.tick_units("/installed/bin/agentihooks")[
        "agentihooks-swarm.timer"
    ]


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


TICK_SERVICE = (
    "[Unit]\nDescription=agentihooks swarm tick\n\n"
    "[Service]\nType=oneshot\n"
    "Environment=PATH=%h/.local/bin:%h/.cargo/bin:/usr/local/bin:/usr/bin:/bin\n"
    "EnvironmentFile=-%h/.agentihooks/.env\n"
    'ExecStart="/installed/bin/agentihooks" swarm tick\n'
    "TimeoutStartSec=540\nKillMode=process\n"
)
TICK_TIMER = (
    "[Unit]\nDescription=agentihooks swarm tick every minute\n\n"
    "[Timer]\nOnBootSec=1min\nOnUnitActiveSec=60s\nAccuracySec=5s\n\n"
    "[Install]\nWantedBy=timers.target\n"
)
NONE_YET = "no scheduler has delivered a swarm tick yet"


def test_the_tick_units_run_the_installed_tick_every_minute():
    assert timer.tick_units("/installed/bin/agentihooks") == {
        "agentihooks-swarm.service": TICK_SERVICE,
        "agentihooks-swarm.timer": TICK_TIMER,
    }


def test_ensure_keeps_the_tick_timer_until_the_installed_scheduler_delivered_a_tick(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(timer, "undelivered", lambda: NONE_YET)
    (tmp_path / "agentihooks-swarm.timer").write_text(TICK_TIMER)
    (tmp_path / "agentihooks-swarm.service").write_text(TICK_SERVICE)
    calls = []
    assert timer.ensure("/installed/bin/agentihooks", tmp_path, run=_systemctl(calls))
    argvs = [argv for argv, _ in calls]
    assert not [argv for argv in argvs if "disable" in argv]
    assert (tmp_path / "agentihooks-swarm.timer").read_text() == TICK_TIMER
    assert (tmp_path / "agentihooks-swarm.service").read_text() == TICK_SERVICE
    assert ["systemctl", "--user", "enable", "--now", "agentihooks-swarm.timer"] in argvs
    assert ["systemctl", "--user", "enable", "--now", "agentihooks-swarm.service"] not in argvs
    assert capsys.readouterr().err == f"the agentihooks-swarm timer stays: {NONE_YET}\n"


def test_ensure_reloads_nothing_while_the_kept_units_are_current(tmp_path, monkeypatch):
    monkeypatch.setattr(timer, "undelivered", lambda: NONE_YET)
    assert timer.ensure("/installed/bin/agentihooks", tmp_path, run=_systemctl([]))
    calls = []
    assert timer.ensure("/installed/bin/agentihooks", tmp_path, run=_systemctl(calls))
    argvs = [argv for argv, _ in calls]
    assert ["systemctl", "--user", "daemon-reload"] not in argvs
    assert ["systemctl", "--user", "enable", "--now", "agentihooks-swarm.timer"] in argvs


def test_ensure_restores_a_retired_tick_timer_while_delivery_fails(tmp_path, monkeypatch):
    calls = []
    assert timer.ensure("/installed/bin/agentihooks", tmp_path, run=_systemctl(calls))
    assert not (tmp_path / "agentihooks-swarm.timer").exists()
    monkeypatch.setattr(timer, "undelivered", lambda: "the last swarm tick was delivered 400 seconds ago")
    calls.clear()
    assert timer.ensure("/installed/bin/agentihooks", tmp_path, run=_systemctl(calls))
    assert (tmp_path / "agentihooks-swarm.timer").read_text() == TICK_TIMER
    assert (tmp_path / "agentihooks-swarm.service").read_text() == TICK_SERVICE
    argvs = [argv for argv, _ in calls]
    assert argvs.index(["systemctl", "--user", "daemon-reload"]) < argvs.index(
        ["systemctl", "--user", "enable", "--now", "agentihooks-swarm.timer"]
    )


def test_an_unreadable_delivery_keeps_the_tick_timer(tmp_path, monkeypatch, capsys):
    from redis.exceptions import ConnectionError

    def down():
        raise ConnectionError("down")

    monkeypatch.setattr(timer, "undelivered", down)
    (tmp_path / "agentihooks-swarm.timer").write_text(TICK_TIMER)
    calls = []
    assert timer.ensure("/installed/bin/agentihooks", tmp_path, run=_systemctl(calls))
    assert (tmp_path / "agentihooks-swarm.timer").exists()
    assert not [argv for argv, _ in calls if "disable" in argv]
    assert capsys.readouterr().err == (
        "the agentihooks-swarm timer stays: the swarm tick delivery could not be read: ConnectionError: down\n"
    )


def test_reconcile_keeps_rerunning_ensure_while_the_tick_timer_is_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(timer, "reconcile", real_reconcile)
    monkeypatch.setattr(timer, "undelivered", lambda: NONE_YET)
    assert timer.ensure("/installed/bin/agentihooks", tmp_path, run=_systemctl([]))
    monkeypatch.setattr(timer, "undelivered", lambda: "")
    calls = []
    assert timer.reconcile("/installed/bin/agentihooks", tmp_path, run=_systemctl(calls))
    assert calls[0][0] == ["systemctl", "--user", "disable", "--now", "agentihooks-swarm.timer"]
    assert not (tmp_path / "agentihooks-swarm.timer").exists()


def test_undelivered_reads_this_machine_against_the_installed_root(monkeypatch):
    import time

    import fakeredis

    from scripts.scheduler.core import Scheduler
    from scripts.swarm import scheduled_tick, store

    redis = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(store, "redis_client", lambda: redis)
    monkeypatch.setattr(time, "time", lambda: 1_000.0)
    assert real_undelivered() == NONE_YET
    scheduled_tick.record(Scheduler(redis), "/installed", 900.0)
    assert real_undelivered() == ""
    scheduled_tick.record(Scheduler(redis), "/main", 900.0)
    assert real_undelivered() == (
        "the last swarm tick was delivered by a scheduler running /main, not the installed /installed"
    )


def test_guard_restarts_a_dead_supervisor_on_every_pass(tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(timer, "supervise", lambda: started.append(True) or True)
    monkeypatch.setattr(timer, "undelivered", lambda: "")
    monkeypatch.setattr(timer, "ensure", lambda *args, **kwargs: pytest.fail("ensure ran"))
    assert real_guard("/installed/bin/agentihooks", tmp_path, run=_systemctl([])) is True
    assert real_guard("/installed/bin/agentihooks", tmp_path, run=_systemctl([])) is True
    assert started == [True, True]


def test_guard_restores_the_tick_timer_once_delivery_goes_stale(tmp_path, monkeypatch):
    ensured = []
    monkeypatch.setattr(timer, "supervise", lambda: True)
    monkeypatch.setattr(timer, "undelivered", lambda: "the last swarm tick was delivered 400 seconds ago")
    monkeypatch.setattr(timer, "ensure", lambda binary, unit_dir, run: ensured.append((binary, unit_dir, run)) or False)
    runner = _systemctl([])
    assert real_guard("/installed/bin/agentihooks", tmp_path, run=runner) is False
    assert ensured == [("/installed/bin/agentihooks", tmp_path, runner)]
    (tmp_path / "agentihooks-swarm.timer").write_text(TICK_TIMER)
    (tmp_path / "agentihooks-swarm.service").write_text(TICK_SERVICE)
    calls = []
    assert real_guard("/installed/bin/agentihooks", tmp_path, run=_systemctl(calls)) is True
    assert len(ensured) == 1
    assert calls == [
        (
            ["systemctl", "--user", "is-enabled", "--quiet", "agentihooks-swarm.timer"],
            {"capture_output": True, "text": True, "timeout": 30},
        )
    ]
    assert real_guard("/installed/bin/agentihooks", tmp_path, run=_systemctl([], failing={"is-enabled"})) is False
    assert len(ensured) == 2


def test_guard_restores_the_tick_timer_when_delivery_cannot_be_read(tmp_path, monkeypatch):
    from redis.exceptions import ConnectionError

    def down():
        raise ConnectionError("down")

    ensured = []
    monkeypatch.setattr(timer, "supervise", lambda: True)
    monkeypatch.setattr(timer, "undelivered", down)
    monkeypatch.setattr(timer, "ensure", lambda binary, unit_dir, run: ensured.append(binary) or True)
    assert real_guard("/installed/bin/agentihooks", tmp_path, run=_systemctl([])) is True
    assert ensured == ["/installed/bin/agentihooks"]


def test_supervise_starts_the_scheduler_with_the_swarm_redis(monkeypatch):
    from scripts.scheduler import service

    seen = []
    monkeypatch.setenv("AGENTIHOOKS_SWARM_REDIS_URL", "redis://swarm:6379/3")
    monkeypatch.setattr(service, "ensure_running", lambda environ: seen.append(environ) or False)
    assert timer.supervise() is False
    assert seen[0]["AGENTIHOOKS_SWARM_REDIS_URL"] == "redis://swarm:6379/3"


def _supervisor(monkeypatch, record, order, drained=True):
    from scripts.scheduler import service
    from scripts.swarm.store import redis_url

    def swarm(environ):
        assert environ["AGENTIHOOKS_SWARM_REDIS_URL"] == redis_url(os.environ)
        return environ

    monkeypatch.setattr(service, "holder", lambda environ: swarm(environ) and record)
    monkeypatch.setattr(service, "revision", lambda root: f"revision at {root}")
    monkeypatch.setattr(
        service, "drain", lambda pid, environ: swarm(environ) and order.append(f"drain {pid}") or drained
    )
    monkeypatch.setattr(timer, "supervise", lambda: order.append("supervise") or True)


CURRENT = {"pid": 4242, "root": "/installed", "revision": "revision at /installed", "started": 1_000.0}


@pytest.mark.parametrize(
    ("record", "why"),
    [
        ({**CURRENT, "root": "/main"}, "the scheduler supervisor 4242 runs /main, not the installed /installed"),
        (
            {**CURRENT, "revision": "older"},
            "the scheduler supervisor 4242 loaded revision older, not the installed revision revision at /installed",
        ),
        ({"pid": 4242}, "the scheduler supervisor 4242 left no record of the code it loaded"),
    ],
)
def test_guard_drains_a_supervisor_off_the_installed_code_and_starts_one_from_it(
    tmp_path, monkeypatch, capsys, record, why
):
    order = []
    _supervisor(monkeypatch, record, order)
    assert real_guard("/installed/bin/agentihooks", tmp_path, run=_systemctl([])) is True
    assert order == ["drain 4242", "supervise"]
    assert capsys.readouterr().err == f"recycling the scheduler supervisor: {why}\n"


@pytest.mark.parametrize("record", [CURRENT, None])
def test_guard_leaves_a_current_or_absent_supervisor_alone(tmp_path, monkeypatch, capsys, record):
    order = []
    _supervisor(monkeypatch, record, order)
    assert real_guard("/installed/bin/agentihooks", tmp_path, run=_systemctl([])) is True
    assert order == ["supervise"]
    assert capsys.readouterr().err == ""


def test_guard_says_when_a_drained_supervisor_holds_on_and_recycles_it_on_a_later_pass(tmp_path, monkeypatch, capsys):
    order = []
    _supervisor(monkeypatch, {**CURRENT, "root": "/main"}, order, drained=False)
    assert real_guard("/installed/bin/agentihooks", tmp_path, run=_systemctl([])) is True
    assert real_guard("/installed/bin/agentihooks", tmp_path, run=_systemctl([])) is True
    assert order == ["drain 4242", "supervise", "drain 4242", "supervise"]
    assert capsys.readouterr().err.splitlines()[1] == (
        "the scheduler supervisor 4242 still holds its lock; the next wait pass tries again"
    )


def _delivering(monkeypatch):
    import fakeredis

    from scripts.scheduler.core import Scheduler
    from scripts.swarm import store

    redis = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(store, "redis_client", lambda: redis)
    return Scheduler(redis)


def _kept_units(unit_dir):
    for name, text in timer.tick_units("/installed/bin/agentihooks").items():
        (unit_dir / name).write_text(text)


@pytest.mark.parametrize(("delivered", "retired"), [(999.0, False), (1_000.0, True), (1_001.0, True)])
def test_guard_retires_a_kept_timer_only_after_the_running_supervisor_delivered(
    tmp_path, monkeypatch, delivered, retired
):
    from scripts.swarm import scheduled_tick

    here = _delivering(monkeypatch)
    _supervisor(monkeypatch, CURRENT, [])
    ensured = []
    monkeypatch.setattr(timer, "ensure", lambda binary, unit_dir, run: ensured.append((binary, unit_dir, run)) or True)
    _kept_units(tmp_path)
    scheduled_tick.record(here, "/installed", delivered)
    runner = _systemctl([])
    assert real_guard("/installed/bin/agentihooks", tmp_path, run=runner) is True
    assert ensured == ([("/installed/bin/agentihooks", tmp_path, runner)] if retired else [])


def test_guard_retires_nothing_once_the_timer_units_are_gone(tmp_path, monkeypatch):
    from scripts.swarm import scheduled_tick

    here = _delivering(monkeypatch)
    _supervisor(monkeypatch, CURRENT, [])
    monkeypatch.setattr(timer, "ensure", lambda *args, **kwargs: pytest.fail("ensure ran"))
    scheduled_tick.record(here, "/installed", 2_000.0)
    assert real_guard("/installed/bin/agentihooks", tmp_path, run=_systemctl([])) is True


@pytest.mark.parametrize("record", [None, {"pid": 4242}])
def test_guard_keeps_the_timer_without_a_recorded_supervisor(tmp_path, monkeypatch, record):
    from scripts.scheduler import service
    from scripts.swarm import scheduled_tick

    here = _delivering(monkeypatch)
    monkeypatch.setattr(service, "holder", lambda environ: record)
    monkeypatch.setattr(timer, "recycle", lambda: None)
    monkeypatch.setattr(timer, "supervise", lambda: True)
    monkeypatch.setattr(timer, "ensure", lambda *args, **kwargs: pytest.fail("the timer was retired"))
    _kept_units(tmp_path)
    scheduled_tick.record(here, "/installed", 5_000.0)
    assert real_guard("/installed/bin/agentihooks", tmp_path, run=_systemctl([])) is True


def test_guard_keeps_the_timer_while_a_tick_runs_in_its_service(tmp_path, monkeypatch):
    from scripts.swarm import scheduled_tick

    here = _delivering(monkeypatch)
    _supervisor(monkeypatch, CURRENT, [])
    monkeypatch.setattr(timer, "ensure", lambda *args, **kwargs: pytest.fail("the timer was retired"))
    _kept_units(tmp_path)
    scheduled_tick.record(here, "/installed", 2_000.0)
    busy = _draining([], [("/user.slice/agentihooks-swarm.service", 2)], [0.0])
    assert real_guard("/installed/bin/agentihooks", tmp_path, run=busy) is True


def test_guard_keeps_the_timer_when_the_delivery_cannot_be_read(tmp_path, monkeypatch):
    from redis.exceptions import ConnectionError

    from scripts.swarm import store

    def down():
        raise ConnectionError("down")

    _supervisor(monkeypatch, CURRENT, [])
    monkeypatch.setattr(store, "redis_client", down)
    monkeypatch.setattr(timer, "ensure", lambda *args, **kwargs: pytest.fail("the timer was retired"))
    _kept_units(tmp_path)
    assert real_guard("/installed/bin/agentihooks", tmp_path, run=_systemctl([])) is True


def test_a_foreign_supervisor_is_recycled_and_the_timer_and_finding_clear_on_its_successor_tick(tmp_path, monkeypatch):
    import time

    from scripts.doctor import tick_delivery
    from scripts.scheduler import service
    from scripts.swarm import scheduled_tick

    here = _delivering(monkeypatch)
    monkeypatch.setattr(timer, "undelivered", real_undelivered)
    monkeypatch.setattr(time, "time", lambda: 1_100.0)
    order = []
    _supervisor(monkeypatch, {**CURRENT, "root": "/main", "started": 10.0}, order)
    ensured = []
    monkeypatch.setattr(timer, "ensure", lambda binary, unit_dir, run: ensured.append(len(order)) or True)
    scheduled_tick.record(here, "/main", 1_090.0)
    assert real_guard("/installed/bin/agentihooks", tmp_path, run=_systemctl([])) is True
    assert order == ["drain 4242", "supervise"]
    assert ensured == [2]
    assert [finding.id for finding in tick_delivery.read()] == [f"scheduled-tick/{tick_delivery.socket.gethostname()}"]
    _kept_units(tmp_path)
    monkeypatch.setattr(service, "holder", lambda environ: {**CURRENT, "started": 1_095.0})
    assert real_guard("/installed/bin/agentihooks", tmp_path, run=_systemctl([])) is True
    assert ensured == [2]
    scheduled_tick.record(here, "/installed", 1_096.0)
    assert tick_delivery.read() == []
    assert real_guard("/installed/bin/agentihooks", tmp_path, run=_systemctl([])) is True
    assert ensured == [2, 4]
    assert order == ["drain 4242", "supervise", "supervise", "supervise"]
