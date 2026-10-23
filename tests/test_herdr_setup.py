import json
import subprocess
import sys
import tomllib
import types

import install
import pytest
import tomlkit

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


def _herdr_hooks(monkeypatch, tmp_path):
    from tests.codex_hook_fixtures import CODEX_HOOKS

    home = tmp_path / "codex-home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    (home / "hooks.json").write_text(json.dumps({"hooks": {"Interrupt": CODEX_HOOKS["hooks"]["Interrupt"][:1]}}))
    return home


def test_configure_trusts_the_codex_hooks_herdr_installed(monkeypatch, runner, tmp_path):
    from tests.codex_hook_fixtures import CODEX_HOOK_HASHES

    home = _herdr_hooks(monkeypatch, tmp_path)
    assert herdr_setup.configure() == 0
    state = tomllib.loads((home / "config.toml").read_text())["hooks"]["state"]
    assert state == {f"{home / 'hooks.json'}:interrupt:0:0": {"trusted_hash": CODEX_HOOK_HASHES["interrupt:0:0"]}}


def test_a_failed_codex_integration_still_trusts_what_it_wrote(monkeypatch, runner, tmp_path):
    from tests.codex_hook_fixtures import CODEX_HOOK_HASHES

    home = _herdr_hooks(monkeypatch, tmp_path)

    def failing(command, **kwargs):
        runner(command, **kwargs)
        return subprocess.CompletedProcess(command, int(command[1:] == ["integration", "install", "codex"]), "", "")

    monkeypatch.setattr(herdr_setup.subprocess, "run", failing)
    assert herdr_setup.configure() == 1
    state = tomllib.loads((home / "config.toml").read_text())["hooks"]["state"]
    assert state == {f"{home / 'hooks.json'}:interrupt:0:0": {"trusted_hash": CODEX_HOOK_HASHES["interrupt:0:0"]}}


def test_integration_files_reads_each_installed_file_from_the_status_paths(runner, tmp_path):
    folder = tmp_path / "home:with colon"
    folder.mkdir()
    (folder / "hook.sh").write_bytes(b"v10")
    runner.status = (
        f"claude: current (v10) ({folder / 'hook.sh'})\n"
        f"codex: not installed ({folder / 'absent.sh'})\n"
        "kimi: failed (exit 1) see log\n"
        "header without paths\n"
    )
    assert herdr_setup.integration_files() == {"claude": b"v10", "codex": None}
    assert runner.options[("/bin/herdr", "integration", "status")] == {"capture_output": True, "text": True}


def test_a_missing_binary_is_installed_before_configuring(monkeypatch, runner):
    present = iter([None])
    monkeypatch.setattr(herdr_setup, "binary", lambda: next(present, "/bin/herdr"))
    herdr_setup.init_step("yes", interactive=False)
    assert runner.calls[0] == ["sh", "-c", herdr_setup.INSTALL_COMMAND]
    assert ["/bin/herdr", "integration", "install", "claude"] in runner.calls


@pytest.mark.parametrize(
    "options, expected", [([], ["claude", "codex"]), (["--with-copilot"], ["claude", "codex", "copilot"])]
)
def test_init_runs_the_herdr_step_once_for_all_targets(options, expected, monkeypatch):
    calls, steps = [], []
    monkeypatch.setattr(install, "cmd_init_unified", lambda args: calls.append(args))
    monkeypatch.setattr(herdr_setup, "init_step", lambda flag, interactive: steps.append(flag))
    monkeypatch.setattr(sys, "argv", ["agentihooks", "init", "--profile", "default", "--herdr", "no", *options])
    install.main()
    assert [args.install_target for args in calls] == expected
    assert steps == ["no"]


def _configured(monkeypatch, runner, tmp_path, existing: str | None) -> str:
    path = tmp_path / "xdg" / "herdr" / "config.toml"
    if existing is not None:
        path.parent.mkdir(parents=True)
        path.write_text(existing, encoding="utf-8")
    monkeypatch.setenv("HERDR_CONFIG_PATH", str(path))
    assert herdr_setup.configure() == 0
    return path.read_text(encoding="utf-8")


RESUME_OFF = "\n[session]\nresume_agents_on_restore = false\n"


def _no_rewrite(doc):
    raise AssertionError("rewrote a compliant config")


def test_configure_adds_a_ui_section_that_turns_copy_on_select_off(monkeypatch, runner, tmp_path, capsys):
    text = _configured(monkeypatch, runner, tmp_path, 'onboarding = false\n\n[theme]\nname = "one-dark"\n')
    assert text == 'onboarding = false\n\n[theme]\nname = "one-dark"\n\n[ui]\ncopy_on_select = false\n' + RESUME_OFF
    assert runner.options[("/bin/herdr", "server", "reload-config")] == {"capture_output": True}
    printed = capsys.readouterr().out.splitlines()[-1]
    path = tmp_path / "xdg" / "herdr" / "config.toml"
    assert printed == f"[herdr] config: copy_on_select = false, resume_agents_on_restore = false in {path}"


def test_configure_merges_copy_on_select_into_an_existing_ui_section(monkeypatch, runner, tmp_path):
    existing = "# mine\n[ui]\nconfirm_close = true\n\n[ui.sound]\nenabled = false\n"
    text = _configured(monkeypatch, runner, tmp_path, existing)
    assert (
        text
        == "# mine\n[ui]\nconfirm_close = true\ncopy_on_select = false\n\n[ui.sound]\nenabled = false\n" + RESUME_OFF
    )
    assert ["/bin/herdr", "server", "reload-config"] in runner.calls


def test_configure_keeps_copy_on_select_the_operator_turned_on(monkeypatch, runner, tmp_path, capsys):
    existing = "[ui]\ncopy_on_select = true\n"
    assert _configured(monkeypatch, runner, tmp_path, existing) == existing + RESUME_OFF
    printed = capsys.readouterr().out.splitlines()[-1]
    assert printed.startswith("[herdr] config: resume_agents_on_restore = false in ")


def test_configure_turns_off_agent_resume_the_operator_turned_on(monkeypatch, runner, tmp_path):
    existing = "[ui]\ncopy_on_select = true\n\n[session]\nresume_agents_on_restore = true\n"
    text = _configured(monkeypatch, runner, tmp_path, existing)
    assert tomllib.loads(text)["session"] == {"resume_agents_on_restore": False}
    assert text == "[ui]\ncopy_on_select = true\n" + RESUME_OFF
    assert ["/bin/herdr", "server", "reload-config"] in runner.calls


def test_configure_leaves_a_compliant_config_alone(monkeypatch, runner, tmp_path, capsys):
    existing = "[ui]\ncopy_on_select = true\n" + RESUME_OFF
    monkeypatch.setattr(herdr_setup, "tomlkit", types.SimpleNamespace(parse=tomlkit.parse, dumps=_no_rewrite))
    assert _configured(monkeypatch, runner, tmp_path, existing) == existing
    assert ["/bin/herdr", "server", "reload-config"] not in runner.calls
    assert "[herdr] config:" not in capsys.readouterr().out


def test_configure_creates_a_missing_config(monkeypatch, runner, tmp_path):
    assert _configured(monkeypatch, runner, tmp_path, None) == "[ui]\ncopy_on_select = false\n" + RESUME_OFF


def test_the_config_path_follows_herdr(monkeypatch, tmp_path):
    monkeypatch.delenv("HERDR_CONFIG_PATH", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert herdr_setup.config_path() == herdr_setup.Path.home() / ".config" / "herdr" / "config.toml"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert herdr_setup.config_path() == tmp_path / "xdg" / "herdr" / "config.toml"
    monkeypatch.setenv("HERDR_CONFIG_PATH", str(tmp_path / "own.toml"))
    assert herdr_setup.config_path() == tmp_path / "own.toml"
