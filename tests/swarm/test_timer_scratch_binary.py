"""A run whose agentihooks binary or interpreter is not the installed CLI leaves the shared swarm units alone."""

import subprocess
import sys
from argparse import Namespace
from collections.abc import Callable
from pathlib import Path

import pytest

from scripts.swarm import cli, timer

pytestmark = pytest.mark.unit

real_reconcile = timer.reconcile
real_guard = timer.guard
WAKER = "agentihooks-inbox-waker.service"


def _runner(calls: list[list[str]]) -> Callable[..., subprocess.CompletedProcess[str]]:
    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    return run


def _venv(bin_dir: Path) -> Path:
    bin_dir.mkdir(parents=True)
    (bin_dir / "agentihooks").write_text("#!/bin/sh\n")
    (bin_dir / "python").write_text("")
    return bin_dir


@pytest.fixture
def shared(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Namespace:
    from scripts.targets._common import _install_module

    install = _install_module()
    root = tmp_path / "proof" / "agentihooks"
    root.mkdir(parents=True)
    monkeypatch.setattr(install, "AGENTIHOOKS_ROOT", root)
    monkeypatch.setattr(install, "install_root", lambda: root)
    installed = _venv(tmp_path / "tool" / "bin")
    link = tmp_path / "local" / "bin" / "agentihooks"
    link.parent.mkdir(parents=True)
    link.symlink_to(installed / "agentihooks")
    monkeypatch.setattr(install, "_cli_link", lambda: link)
    scratch = _venv(tmp_path / "proof" / ".venv" / "bin")
    units = tmp_path / "systemd" / "user"
    units.mkdir(parents=True)
    (units / WAKER).write_text(f'ExecStart="{link}" swarm waker\n')
    monkeypatch.setattr(timer, "UNIT_DIR", units)
    monkeypatch.setattr(sys, "executable", str(installed / "python"))
    return Namespace(units=units, installed=installed, scratch=scratch, link=link)


def _snapshot(units: Path) -> dict[str, str]:
    return {path.name: path.read_text() for path in units.iterdir()}


def _refusal(path: Path, shared: Namespace) -> str:
    return (
        f"this run uses {path}, not the installed agentihooks at {shared.link}, "
        f"so it leaves the shared swarm timer units in {shared.units} alone\n"
    )


@pytest.mark.feature("shared-swarm-units-installed-binary")
def test_ensure_refuses_a_scratch_binary_although_the_running_root_reads_as_installed(
    shared: Namespace, capsys: pytest.CaptureFixture[str]
) -> None:
    before, calls = _snapshot(shared.units), []
    binary = shared.scratch / "agentihooks"
    assert timer.ensure(str(binary), run=_runner(calls)) is False
    assert _snapshot(shared.units) == before
    assert calls == []
    assert capsys.readouterr().err == _refusal(binary, shared)


@pytest.mark.feature("shared-swarm-units-installed-binary")
def test_ensure_refuses_the_installed_binary_run_by_a_scratch_interpreter(
    shared: Namespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    before, calls = _snapshot(shared.units), []
    monkeypatch.setattr(sys, "executable", str(shared.scratch / "python"))
    assert timer.ensure(str(shared.link), run=_runner(calls)) is False
    assert _snapshot(shared.units) == before
    assert calls == []
    assert capsys.readouterr().err == _refusal(shared.scratch / "python", shared)


@pytest.mark.feature("shared-swarm-units-installed-binary")
def test_reconcile_refuses_a_scratch_binary_with_stale_or_current_units(
    shared: Namespace, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[list[str]] = []
    binary = shared.scratch / "agentihooks"
    before = _snapshot(shared.units)
    assert real_reconcile(str(binary), run=_runner(calls)) is False
    assert _snapshot(shared.units) == before
    for name, text in timer.units(str(binary)).items():
        (shared.units / name).write_text(text)
    current = _snapshot(shared.units)
    assert real_reconcile(str(binary), run=_runner(calls)) is False
    assert _snapshot(shared.units) == current
    assert calls == []
    assert capsys.readouterr().err == _refusal(binary, shared) * 2


@pytest.mark.feature("shared-swarm-units-installed-binary")
def test_guard_from_a_scratch_binary_leaves_the_units_and_the_scheduler_alone(
    shared: Namespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(timer, "recycle", lambda: pytest.fail("the scheduler supervisor was recycled"))
    monkeypatch.setattr(timer, "supervise", lambda: pytest.fail("the scheduler supervisor was started"))
    monkeypatch.setattr(timer, "undelivered", lambda: "the last swarm tick was delivered 400 seconds ago")
    before, calls = _snapshot(shared.units), []
    binary = shared.scratch / "agentihooks"
    assert real_guard(str(binary), run=_runner(calls)) is False
    assert _snapshot(shared.units) == before
    assert calls == []
    assert capsys.readouterr().err == _refusal(binary, shared)


@pytest.mark.feature("shared-swarm-units-installed-binary")
def test_reconcile_refuses_the_installed_binary_run_from_a_foreign_root(
    shared: Namespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts.targets._common import _install_module

    install = _install_module()
    primary = tmp_path / "primary"
    monkeypatch.setattr(install, "install_root", lambda: primary)
    before, calls = _snapshot(shared.units), []
    assert real_reconcile(str(shared.link), run=_runner(calls)) is False
    assert _snapshot(shared.units) == before
    assert calls == []
    running = install.AGENTIHOOKS_ROOT.resolve()
    assert capsys.readouterr().err == (
        f"this run comes from {running}, not the installed agentihooks at {primary.resolve()}, "
        f"so it leaves the shared swarm timer units in {shared.units} alone\n"
    )


@pytest.mark.feature("shared-swarm-units-installed-binary")
def test_a_spoke_workstation_with_a_scratch_binary_leaves_the_units_alone(
    shared: Namespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts.hive import workstation

    monkeypatch.setattr(workstation, "runs_engine", lambda environ: False)
    before, calls = _snapshot(shared.units), []
    binary = shared.scratch / "agentihooks"
    assert timer.ensure(str(binary), run=_runner(calls)) is False
    assert _snapshot(shared.units) == before
    assert calls == []
    assert capsys.readouterr().err == _refusal(binary, shared)


class _Store:
    state = "paused"
    redis = Namespace(delete=lambda key: None)

    def key(self, slug: str, name: str) -> str:
        return name

    def update(self, slug: str, state: str) -> None:
        self.state = state

    def config(self, slug: str) -> Namespace:
        return Namespace(state=self.state)


@pytest.mark.feature("shared-swarm-units-installed-binary")
def test_a_swarm_started_from_a_scratch_venv_leaves_the_shared_units_unchanged(
    shared: Namespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(timer.sysconfig, "get_path", lambda name: str(shared.scratch))
    monkeypatch.setattr(sys, "executable", str(shared.scratch / "python"))
    monkeypatch.setattr(cli, "run_tick", lambda store, slug: [])
    before = _snapshot(shared.units)
    cli._state(_Store(), Namespace(slug="sw"), "running")
    assert _snapshot(shared.units) == before
    assert _refusal(shared.scratch / "agentihooks", shared) in capsys.readouterr().err


@pytest.mark.feature("shared-swarm-units-installed-binary")
def test_the_installed_binary_and_interpreter_still_write_the_shared_units(shared: Namespace) -> None:
    calls: list[list[str]] = []
    assert timer.ensure(str(shared.link), run=_runner(calls)) is True
    assert _snapshot(shared.units)[WAKER] == timer.units(str(shared.link))[WAKER]
    assert ["systemctl", "--user", "enable", "--now", WAKER] in calls


@pytest.mark.feature("shared-swarm-units-installed-binary")
def test_a_proof_unit_dir_still_takes_a_scratch_binary(shared: Namespace, tmp_path: Path) -> None:
    proof = tmp_path / "proof-units"
    binary = str(shared.scratch / "agentihooks")
    assert timer.ensure(binary, proof, run=_runner([])) is True
    assert (proof / WAKER).read_text() == timer.units(binary)[WAKER]


@pytest.mark.feature("shared-swarm-units-installed-binary")
def test_a_missing_installed_cli_link_refuses_every_binary(
    shared: Namespace, capsys: pytest.CaptureFixture[str]
) -> None:
    shared.link.unlink()
    before = _snapshot(shared.units)
    assert timer.ensure(str(shared.installed / "agentihooks"), run=_runner([])) is False
    assert _snapshot(shared.units) == before
    assert capsys.readouterr().err == _refusal(shared.installed / "agentihooks", shared)
