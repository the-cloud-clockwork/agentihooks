import subprocess
import sys

import install
import pytest

from scripts import herdr_setup


class Runner:
    def __init__(self, status: str = "", writes: dict | None = None):
        self.calls: list[list[str]] = []
        self.options: dict[tuple[str, ...], dict] = {}
        self.status = status
        self.writes = writes or {}

    def __call__(self, command, **kwargs):
        self.calls.append(list(command))
        self.options[tuple(command)] = kwargs
        if command[1:3] == ["integration", "install"] and command[3] in self.writes:
            path, data = self.writes[command[3]]
            path.write_bytes(data)
        out = self.status if command[1:] == ["integration", "status"] else ""
        return subprocess.CompletedProcess(command, 0, out, "")


@pytest.fixture
def runner(monkeypatch, tmp_path):
    monkeypatch.setenv("HERDR_CONFIG_PATH", str(tmp_path / "herdr-config.toml"))
    claude, codex = tmp_path / "claude-hook.sh", tmp_path / "codex-hook.sh"
    codex.write_bytes(b"v8")
    run = Runner(
        f"claude: not installed ({claude})\ncodex: current (v8) ({codex})\n",
        {"claude": (claude, b"v10"), "codex": (codex, b"v8")},
    )
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


def test_an_unchanged_reinstall_records_no_session_affecting_change(monkeypatch, runner, tmp_path):
    claude, codex = tmp_path / "claude-hook.sh", tmp_path / "codex-hook.sh"
    claude.write_bytes(b"v10")
    runner.status = f"claude: outdated (v9) ({claude})\ncodex: unknown ({codex})\n"
    assert herdr_setup.configure() == 0
    assert ["/bin/herdr", "integration", "install", "claude"] in runner.calls
    assert runner.marked == []


def test_a_rewritten_integration_file_marks_sessions_changed(runner, tmp_path):
    (tmp_path / "claude-hook.sh").write_bytes(b"v9")
    assert herdr_setup.configure() == 0
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


def _configured(monkeypatch, runner, tmp_path, existing: str | None) -> str:
    path = tmp_path / "xdg" / "herdr" / "config.toml"
    if existing is not None:
        path.parent.mkdir(parents=True)
        path.write_text(existing, encoding="utf-8")
    monkeypatch.setenv("HERDR_CONFIG_PATH", str(path))
    assert herdr_setup.configure() == 0
    return path.read_text(encoding="utf-8")


def test_configure_adds_a_ui_section_that_turns_copy_on_select_off(monkeypatch, runner, tmp_path, capsys):
    text = _configured(monkeypatch, runner, tmp_path, 'onboarding = false\n\n[theme]\nname = "one-dark"\n')
    assert text == 'onboarding = false\n\n[theme]\nname = "one-dark"\n\n[ui]\ncopy_on_select = false\n'
    assert runner.options[("/bin/herdr", "server", "reload-config")] == {"capture_output": True}
    printed = capsys.readouterr().out.splitlines()[-1]
    assert printed == f"[herdr] config: copy_on_select = false in {tmp_path / 'xdg' / 'herdr' / 'config.toml'}"


def test_configure_merges_copy_on_select_into_an_existing_ui_section(monkeypatch, runner, tmp_path):
    existing = "# mine\n[ui]\nconfirm_close = true\n\n[ui.sound]\nenabled = false\n"
    text = _configured(monkeypatch, runner, tmp_path, existing)
    assert text == "# mine\n[ui]\nconfirm_close = true\ncopy_on_select = false\n\n[ui.sound]\nenabled = false\n"
    assert ["/bin/herdr", "server", "reload-config"] in runner.calls


def test_configure_keeps_copy_on_select_the_operator_turned_on(monkeypatch, runner, tmp_path):
    existing = "[ui]\ncopy_on_select = true\n"
    assert _configured(monkeypatch, runner, tmp_path, existing) == existing
    assert ["/bin/herdr", "server", "reload-config"] not in runner.calls


def test_configure_creates_a_missing_config(monkeypatch, runner, tmp_path):
    assert _configured(monkeypatch, runner, tmp_path, None) == "[ui]\ncopy_on_select = false\n"


def test_the_config_path_follows_herdr(monkeypatch, tmp_path):
    monkeypatch.delenv("HERDR_CONFIG_PATH", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert herdr_setup.config_path() == herdr_setup.Path.home() / ".config" / "herdr" / "config.toml"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert herdr_setup.config_path() == tmp_path / "xdg" / "herdr" / "config.toml"
    monkeypatch.setenv("HERDR_CONFIG_PATH", str(tmp_path / "own.toml"))
    assert herdr_setup.config_path() == tmp_path / "own.toml"
