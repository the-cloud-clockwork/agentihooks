import subprocess
from argparse import Namespace

import pytest

from scripts.swarm import cli, timer
from scripts.swarm.store import SwarmError

BINARY = "/installed/bin/agentihooks"
real_reconcile = timer.reconcile


def systemctl(calls):
    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    return run


@pytest.fixture
def distributed(monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_DEPLOYMENT", "distributed")
    monkeypatch.setattr(timer, "schedule", lambda: pytest.fail("the swarm tick was scheduled"))
    monkeypatch.setattr(timer, "undelivered", lambda: pytest.fail("the tick delivery was read"))


def test_the_spoke_unit_runs_the_installed_spoke_and_restarts():
    assert timer.spoke_units(BINARY) == {
        "agentihooks-spoke.service": (
            "[Unit]\nDescription=agentihooks workstation spoke for master work\n\n"
            "[Service]\nType=simple\nEnvironment=PYTHONUNBUFFERED=1\n"
            "Environment=PATH=%h/.local/bin:%h/.cargo/bin:/usr/local/bin:/usr/bin:/bin\n"
            "EnvironmentFile=-%h/.agentihooks/.env\n"
            f'ExecStart="{BINARY}" swarm spoke\n'
            "Restart=always\nRestartSec=5\n\n"
            "[Install]\nWantedBy=default.target\n"
        )
    }


def test_a_distributed_install_writes_only_the_spoke_and_schedules_no_tick(tmp_path, distributed):
    calls = []

    assert timer.ensure(BINARY, tmp_path, run=systemctl(calls))

    assert sorted(path.name for path in tmp_path.glob("agentihooks-*")) == ["agentihooks-spoke.service"]
    assert (tmp_path / "agentihooks-spoke.service").read_text() == timer.spoke_units(BINARY)[
        "agentihooks-spoke.service"
    ]
    assert calls == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", "agentihooks-spoke.service"],
    ]


def test_retiring_an_engine_unit_beside_a_current_spoke_still_reloads(tmp_path, distributed):
    timer.ensure(BINARY, tmp_path, run=systemctl([]))
    (tmp_path / "agentihooks-inbox-waker.service").write_text("[Unit]\n")
    calls = []

    assert timer.ensure(BINARY, tmp_path, run=systemctl(calls))

    assert calls == [
        ["systemctl", "--user", "disable", "--now", "agentihooks-inbox-waker.service"],
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", "agentihooks-spoke.service"],
    ]


def test_every_systemctl_call_captures_text_output_within_thirty_seconds(tmp_path, distributed):
    (tmp_path / "agentihooks-swarm.timer").write_text("[Timer]\n")
    seen = []

    def run(argv, **kwargs):
        seen.append(kwargs)
        return subprocess.CompletedProcess(argv, 0, "", "")

    timer.ensure(BINARY, tmp_path, run=run)

    unchecked = {"capture_output": True, "text": True, "timeout": 30, "check": False}
    assert seen == [unchecked, unchecked, {"capture_output": True, "text": True, "timeout": 30}]


def test_a_distributed_install_retires_every_engine_unit_it_finds(tmp_path, distributed):
    engine = [*timer.units(BINARY), *timer.tick_units(BINARY), *timer.DROPPED]
    for name in engine:
        (tmp_path / name).write_text("[Unit]\n")
    calls = []

    assert timer.ensure(BINARY, tmp_path, run=systemctl(calls))

    assert sorted(path.name for path in tmp_path.glob("agentihooks-*")) == ["agentihooks-spoke.service"]
    assert calls[: len(engine)] == [["systemctl", "--user", "disable", "--now", name] for name in engine]
    assert calls[len(engine) :] == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", "agentihooks-spoke.service"],
    ]


def test_a_current_distributed_install_reloads_nothing_and_reports_the_spoke_enable(tmp_path, distributed):
    timer.ensure(BINARY, tmp_path, run=systemctl([]))
    calls = []

    def failing(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, "", "")

    assert timer.ensure(BINARY, tmp_path, run=failing) is False
    assert calls == [["systemctl", "--user", "enable", "--now", "agentihooks-spoke.service"]]


def test_a_distributed_reconcile_installs_the_spoke(tmp_path, monkeypatch, distributed):
    monkeypatch.setattr(timer, "reconcile", real_reconcile)
    (tmp_path / "agentihooks-inbox-waker.service").write_text("[Unit]\n")

    assert timer.reconcile(BINARY, tmp_path, run=systemctl([]))

    assert sorted(path.name for path in tmp_path.glob("agentihooks-*")) == ["agentihooks-spoke.service"]


def test_a_distributed_reconcile_retires_current_engine_units_instead_of_scheduling(tmp_path, monkeypatch, distributed):
    monkeypatch.setattr(timer, "reconcile", real_reconcile)
    for name, text in timer.units(BINARY).items():
        (tmp_path / name).write_text(text)

    assert timer.reconcile(BINARY, tmp_path, run=systemctl([]))

    assert sorted(path.name for path in tmp_path.glob("agentihooks-*")) == ["agentihooks-spoke.service"]


def test_a_distributed_tick_ticks_no_swarm_and_names_the_hub(monkeypatch, capsys, distributed):
    monkeypatch.setattr(cli, "_tick_host", lambda store, started: pytest.fail("the workstation engine ran"))

    class Store:
        def slugs(self):
            pytest.fail("the tick read a swarm")

    cli.cmd_tick(Store(), Namespace())

    assert "the hub runs the swarm engine in distributed mode" in capsys.readouterr().err


def test_the_spoke_command_is_refused_where_the_workstation_runs_the_engine(monkeypatch):
    monkeypatch.delenv("AGENTIHOOKS_DEPLOYMENT", raising=False)

    with pytest.raises(SwarmError, match="the workstation spoke runs only in distributed mode"):
        cli.cmd_spoke(object(), Namespace())


def test_the_spoke_command_serves_master_work_as_this_hive_and_closes_the_bus(monkeypatch, distributed):
    from scripts.events import factory
    from scripts.hive import workstation
    from scripts.swarm import commands

    bus, served = type("Bus", (), {"closed": False, "close": lambda self: setattr(self, "closed", True)})(), []
    configs = []
    monkeypatch.setenv("AGENTIHOOKS_BUS", "nats")
    monkeypatch.setenv("AGENTIHOOKS_BUS_URL", "nats://bus:4222")
    monkeypatch.setattr(factory, "open_bus", lambda config: configs.append(config) or bus)
    monkeypatch.setattr(commands, "hive_id", lambda: "workstation-1")
    monkeypatch.setattr(workstation, "run", lambda *args: served.append(args))
    store = object()

    cli.cmd_spoke(store, Namespace())

    [(seen_store, seen_bus, runtime, hive)] = served
    assert (seen_store, seen_bus, hive, type(runtime).__name__) == (store, bus, "workstation-1", "LocalHerdrRuntime")
    assert isinstance(runtime.herdr, cli.HerdrRuntime)
    assert [(c.backend, c.url) for c in configs] == [("nats", "nats://bus:4222")]
    assert bus.closed


def test_the_spoke_command_dispatches_without_a_swarm_name():
    assert "spoke" in cli.TOP_LEVEL
