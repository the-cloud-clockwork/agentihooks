import subprocess
import sys

import install
import pytest

from scripts import herdr_setup


class Runner:
    def __init__(self, status: str = ""):
        self.calls: list[list[str]] = []
        self.status = status

    def __call__(self, command, **kwargs):
        self.calls.append(list(command))
        out = self.status if command[1:] == ["integration", "status"] else ""
        return subprocess.CompletedProcess(command, 0, out, "")


@pytest.fixture
def runner(monkeypatch):
    run = Runner("claude: not installed (/x)\ncodex: current (v8) (/y)\n")
    monkeypatch.setattr(herdr_setup.subprocess, "run", run)
    monkeypatch.setattr(herdr_setup, "binary", lambda: "/bin/herdr")
    marked = []
    monkeypatch.setattr("scripts.deps_preflight.main", lambda argv: marked.append(argv) or 0)
    run.marked = marked
    return run


def _no_input(prompt=""):
    raise AssertionError("asked again")


def test_the_first_init_asks_once_and_remembers(monkeypatch, runner):
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    herdr_setup.init_step("", interactive=True)
    assert herdr_setup.choice() is True
    monkeypatch.setattr("builtins.input", _no_input)
    herdr_setup.init_step("", interactive=True)
    assert herdr_setup.choice() is True


def test_declining_installs_nothing_and_is_remembered(monkeypatch, runner):
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    herdr_setup.init_step("", interactive=True)
    assert herdr_setup.choice() is False
    assert runner.calls == []
    monkeypatch.setattr("builtins.input", _no_input)
    herdr_setup.init_step("", interactive=True)


def test_without_a_terminal_or_flag_nothing_is_decided(monkeypatch, runner):
    monkeypatch.setattr("builtins.input", _no_input)
    herdr_setup.init_step("", interactive=False)
    assert herdr_setup.choice() is None
    assert runner.calls == []


def test_the_flag_decides_for_an_agent(runner):
    herdr_setup.init_step("no", interactive=False)
    assert herdr_setup.choice() is False
    herdr_setup.init_step("yes", interactive=False)
    assert herdr_setup.choice() is True


def test_enabled_init_installs_the_claude_and_codex_integrations(runner):
    herdr_setup.init_step("yes", interactive=False)
    assert ["/bin/herdr", "integration", "install", "claude"] in runner.calls
    assert ["/bin/herdr", "integration", "install", "codex"] in runner.calls
    assert runner.marked == [["mark-changed", "--reason", "herdr-integration:claude"]]


def test_a_missing_binary_is_installed_before_configuring(monkeypatch, runner):
    present = iter([None])
    monkeypatch.setattr(herdr_setup, "binary", lambda: next(present, "/bin/herdr"))
    herdr_setup.init_step("yes", interactive=False)
    assert runner.calls[0] == ["sh", "-c", herdr_setup.INSTALL_COMMAND]
    assert ["/bin/herdr", "integration", "install", "claude"] in runner.calls


def test_init_runs_the_herdr_step_once_for_all_targets(monkeypatch):
    calls, steps = [], []
    monkeypatch.setattr(install, "cmd_init_unified", lambda args: calls.append(args))
    monkeypatch.setattr(herdr_setup, "init_step", lambda flag, interactive: steps.append(flag))
    monkeypatch.setattr(sys, "argv", ["agentihooks", "init", "--profile", "default", "--herdr", "no"])
    install.main()
    assert len(calls) == len(install.SUPPORTED_TARGETS)
    assert steps == ["no"]
