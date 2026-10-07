import subprocess
from argparse import Namespace

import pytest

from scripts.swarm import cli, timer
from scripts.swarm.store import SwarmError


@pytest.fixture
def installed(tmp_path):
    root = tmp_path / "agentihooks"
    bin_dir = tmp_path / "tool" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "agentihooks").write_text("#!/bin/sh\n")
    return root, bin_dir


def _ok(argv, **kw):
    return subprocess.CompletedProcess(argv, 0)


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
    assert timer.entry_point(bin_dir, which=lambda name: str(link)) == str(link)


def test_a_run_from_a_worktree_is_refused_and_names_both_roots(monkeypatch, tmp_path):
    worktree, primary = tmp_path / "worktree", tmp_path / "primary"
    monkeypatch.setattr(timer, "_roots", lambda: (worktree, primary))
    why = timer.installed_refusal()
    assert str(worktree) in why and str(primary) in why and "installed agentihooks" in why


def test_a_run_from_the_installed_root_is_not_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(timer, "_roots", lambda: (tmp_path, tmp_path))
    assert timer.installed_refusal() == ""


def test_a_non_installed_run_leaves_the_unit_untouched(monkeypatch, tmp_path):
    unit = tmp_path / "agentihooks-swarm.service"
    unit.write_text("original\n")
    monkeypatch.setattr(timer, "_roots", lambda: (tmp_path / "worktree", tmp_path / "primary"))
    calls = []
    why = timer.ensure_installed(tmp_path, run=lambda argv, **kw: calls.append(argv) or _ok(argv))
    assert why and unit.read_text() == "original\n" and calls == []


def test_an_installed_run_writes_the_installed_entry_point(monkeypatch, installed, tmp_path):
    root, bin_dir = installed
    monkeypatch.setattr(timer, "_roots", lambda: (root, root))
    monkeypatch.setattr(timer, "entry_point", lambda: str(bin_dir / "agentihooks"))
    assert timer.ensure_installed(tmp_path / "units", run=_ok) == ""
    service = (tmp_path / "units" / "agentihooks-swarm.service").read_text()
    assert f'ExecStart="{bin_dir / "agentihooks"}" swarm tick' in service


def test_starting_from_a_non_installed_run_says_why_and_writes_nothing(monkeypatch, capsys):
    monkeypatch.setattr(timer, "installed_refusal", lambda: "this run comes from a worktree")
    monkeypatch.setattr(timer, "ensure", lambda *a, **kw: pytest.fail("the unit was written"))
    monkeypatch.setattr(cli, "run_tick", lambda store, slug: [])

    class Store:
        state = "paused"
        redis = Namespace(delete=lambda key: None)

        def key(self, slug, name):
            return name

        def update(self, slug, state):
            self.state = state

        def config(self, slug):
            return Namespace(state=self.state)

    cli._state(Store(), Namespace(slug="sw"), "running")
    err = capsys.readouterr().err
    assert "this run comes from a worktree" in err and "timer" in err


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
