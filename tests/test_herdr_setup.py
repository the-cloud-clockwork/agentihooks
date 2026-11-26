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


SETUP_BINARY = herdr_setup.binary


def _missing(monkeypatch):
    installs = []
    monkeypatch.setattr(herdr_setup, "binary", lambda: None)
    monkeypatch.setattr(herdr_setup, "install", lambda: installs.append("install") or 1)
    return installs


def test_the_first_init_asks_once_and_remembers(monkeypatch, runner):
    installs = _missing(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    herdr_setup.init_step("", interactive=True)
    assert herdr_setup.choice() is True
    assert installs == ["install"]
    monkeypatch.setattr("builtins.input", _no_input)
    herdr_setup.init_step("", interactive=True)
    assert herdr_setup.choice() is True


def test_declining_installs_nothing_and_is_remembered(monkeypatch, runner):
    installs = _missing(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    herdr_setup.init_step("", interactive=True)
    assert herdr_setup.choice() is False
    assert installs == [] and runner.calls == []
    monkeypatch.setattr("builtins.input", _no_input)
    herdr_setup.init_step("", interactive=True)


def test_without_a_terminal_or_flag_nothing_is_decided(monkeypatch, runner):
    installs = _missing(monkeypatch)
    monkeypatch.setattr("builtins.input", _no_input)
    herdr_setup.init_step("", interactive=False)
    assert herdr_setup.choice() is None
    assert installs == [] and runner.calls == []


def test_the_flag_decides_for_an_agent(monkeypatch, runner):
    installs = _missing(monkeypatch)
    herdr_setup.init_step("no", interactive=False)
    assert herdr_setup.choice() is False and installs == []
    herdr_setup.init_step("yes", interactive=False)
    assert herdr_setup.choice() is True and installs == ["install"]


@pytest.mark.feature("herdr-enabled-when-installed")
@pytest.mark.parametrize(("flag", "recorded"), [("", False), ("no", None), ("", None), ("no", True)])
def test_an_installed_herdr_is_configured_whatever_was_answered(monkeypatch, runner, flag, recorded):
    if recorded is not None:
        herdr_setup.record(recorded)
    monkeypatch.setattr("builtins.input", _no_input)
    herdr_setup.init_step(flag, interactive=True)
    assert ["/bin/herdr", "integration", "install", "claude"] in runner.calls


@pytest.mark.feature("herdr-enabled-when-installed")
@pytest.mark.parametrize(("exe", "line", "code"), [("/bin/herdr", "enabled=true", 0), (None, "enabled=false", 1)])
def test_status_reports_herdr_enabled_while_it_is_installed(monkeypatch, runner, capsys, exe, line, code):
    herdr_setup.record(exe is None)
    monkeypatch.setattr(herdr_setup, "binary", lambda: exe)
    assert herdr_setup.status() == code
    assert line in capsys.readouterr().out.splitlines()


@pytest.mark.feature("herdr-enabled-when-installed")
def test_an_installed_herdr_cannot_be_disabled(runner):
    with pytest.raises(SystemExit) as refused:
        herdr_setup.main(["disable"])
    assert refused.value.code == 2
    assert herdr_setup.choice() is None


@pytest.mark.feature("herdr-enabled-when-installed")
def test_setup_finds_herdr_where_the_terminal_host_does(monkeypatch):
    monkeypatch.setattr("scripts.herdr_host.binary", lambda: "/home/u/.local/bin/herdr")
    assert SETUP_BINARY() == "/home/u/.local/bin/herdr"


@pytest.mark.feature("herdr-native-resume")
def test_recording_a_choice_keeps_the_resume_release(runner):
    state = install._load_state()
    state["herdr"] = {"enabled": True, "decided_at": "2026-10-26T18:54:57+00:00", "resume_released": "kept"}
    install._save_state(state)
    herdr_setup.record(False)
    herdr = install._load_state()["herdr"]
    assert (herdr["enabled"], herdr["resume_released"]) == (False, "kept")
    assert herdr["decided_at"] != "2026-10-26T18:54:57+00:00" and herdr["decided_at"].endswith("+00:00")


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
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(existing, encoding="utf-8")
    monkeypatch.setenv("HERDR_CONFIG_PATH", str(path))
    assert herdr_setup.configure() == 0
    return path.read_text(encoding="utf-8")


RESUME_OFF = "\n[session]\nresume_agents_on_restore = false\n"
RESUME_ON = "\n[session]\nresume_agents_on_restore = true\n"


def _no_rewrite(doc):
    raise AssertionError("rewrote a compliant config")


def test_configure_adds_a_ui_section_that_turns_copy_on_select_off(monkeypatch, runner, tmp_path, capsys):
    text = _configured(monkeypatch, runner, tmp_path, 'onboarding = false\n\n[theme]\nname = "one-dark"\n')
    assert text == 'onboarding = false\n\n[theme]\nname = "one-dark"\n\n[ui]\ncopy_on_select = false\n'
    assert runner.options[("/bin/herdr", "server", "reload-config")] == {"capture_output": True}
    printed = capsys.readouterr().out.splitlines()[-1]
    path = tmp_path / "xdg" / "herdr" / "config.toml"
    assert printed == f"[herdr] config: copy_on_select = false in {path}"


def test_configure_merges_copy_on_select_into_an_existing_ui_section(monkeypatch, runner, tmp_path):
    existing = "# mine\n[ui]\nconfirm_close = true\n\n[ui.sound]\nenabled = false\n"
    text = _configured(monkeypatch, runner, tmp_path, existing)
    assert text == "# mine\n[ui]\nconfirm_close = true\ncopy_on_select = false\n\n[ui.sound]\nenabled = false\n"
    assert ["/bin/herdr", "server", "reload-config"] in runner.calls


@pytest.mark.feature("herdr-native-resume")
def test_configure_keeps_agent_resume_the_operator_turned_on(monkeypatch, runner, tmp_path, capsys):
    existing = "[ui]\ncopy_on_select = true\n" + RESUME_ON
    monkeypatch.setattr(herdr_setup, "tomlkit", types.SimpleNamespace(parse=tomlkit.parse, dumps=_no_rewrite))
    assert _configured(monkeypatch, runner, tmp_path, existing) == existing
    assert ["/bin/herdr", "server", "reload-config"] not in runner.calls
    assert "[herdr] config:" not in capsys.readouterr().out


@pytest.mark.feature("herdr-native-resume")
def test_configure_releases_the_resume_off_it_wrote_before_once(monkeypatch, runner, tmp_path, capsys):
    existing = "[ui]\ncopy_on_select = true\n" + RESUME_OFF + "\n[keys]\ngoto = 1\n"
    assert _configured(monkeypatch, runner, tmp_path, existing) == "[ui]\ncopy_on_select = true\n\n[keys]\ngoto = 1\n"
    assert ["/bin/herdr", "server", "reload-config"] in runner.calls
    assert (
        capsys.readouterr()
        .out.splitlines()[-1]
        .startswith("[herdr] config: resume_agents_on_restore restored to the herdr default in ")
    )
    assert install._load_state()["herdr"]["resume_released"].endswith("+00:00")


@pytest.mark.feature("herdr-native-resume")
def test_configure_keeps_a_resume_off_set_after_the_release(monkeypatch, runner, tmp_path):
    _configured(monkeypatch, runner, tmp_path, "[ui]\ncopy_on_select = true\n")
    existing = "[ui]\ncopy_on_select = true\n" + RESUME_OFF
    monkeypatch.setattr(herdr_setup, "tomlkit", types.SimpleNamespace(parse=tomlkit.parse, dumps=_no_rewrite))
    assert _configured(monkeypatch, runner, tmp_path, existing) == existing


@pytest.mark.feature("herdr-native-resume")
def test_configure_keeps_a_resume_off_sharing_its_section(monkeypatch, runner, tmp_path):
    existing = (
        "[ui]\ncopy_on_select = true\n\n[session]\nresume_agents_on_restore = false\nstartup_per_agent_delay_ms = 200\n"
    )
    monkeypatch.setattr(herdr_setup, "tomlkit", types.SimpleNamespace(parse=tomlkit.parse, dumps=_no_rewrite))
    assert _configured(monkeypatch, runner, tmp_path, existing) == existing
    assert install._load_state()["herdr"]["resume_released"]


@pytest.mark.feature("herdr-native-resume")
@pytest.mark.parametrize("value", ["off", "false", "0", "no", " OFF "])
def test_the_resume_switch_turns_agent_resume_off(monkeypatch, runner, tmp_path, value):
    monkeypatch.setenv(herdr_setup.RESUME_ENV, value)
    text = _configured(monkeypatch, runner, tmp_path, "[ui]\ncopy_on_select = true\n" + RESUME_ON)
    assert text == "[ui]\ncopy_on_select = true\n" + RESUME_OFF
    assert ["/bin/herdr", "server", "reload-config"] in runner.calls


@pytest.mark.feature("herdr-native-resume")
def test_the_resume_switch_leaves_a_resume_off_config_alone(monkeypatch, runner, tmp_path, capsys):
    monkeypatch.setenv(herdr_setup.RESUME_ENV, "off")
    existing = "[ui]\ncopy_on_select = true\n" + RESUME_OFF
    monkeypatch.setattr(herdr_setup, "tomlkit", types.SimpleNamespace(parse=tomlkit.parse, dumps=_no_rewrite))
    assert _configured(monkeypatch, runner, tmp_path, existing) == existing
    assert ["/bin/herdr", "server", "reload-config"] not in runner.calls
    assert "[herdr] config:" not in capsys.readouterr().out


@pytest.mark.feature("herdr-native-resume")
@pytest.mark.parametrize("value", ["", "on", "true", "1"])
def test_resume_stays_on_unless_switched_off(value):
    assert herdr_setup.resume_off({herdr_setup.RESUME_ENV: value}) is False


@pytest.mark.feature("herdr-native-resume")
@pytest.mark.parametrize("value", ["off", "false", "0", "no", " Off "])
def test_a_given_environment_switches_resume_off(value):
    assert herdr_setup.resume_off({herdr_setup.RESUME_ENV: value}) is True


@pytest.mark.feature("herdr-native-resume")
def test_resume_stays_on_when_nothing_sets_the_switch(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    assert herdr_setup.resume_off({}) is False
    assert herdr_setup.resume_off({"OTHER": "off"}) is False
    assert herdr_setup.resume_off() is False


@pytest.mark.feature("herdr-native-resume")
def test_the_resume_switch_is_read_from_the_agentihooks_env_file(monkeypatch, tmp_path):
    env_file = tmp_path / ".agentihooks" / ".env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text(f"{herdr_setup.RESUME_ENV}=off\n", encoding="utf-8")
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(env_file.parent))
    assert herdr_setup.resume_off() is True
    monkeypatch.setenv(herdr_setup.RESUME_ENV, "on")
    assert herdr_setup.resume_off() is False


def test_configure_leaves_a_compliant_config_alone(monkeypatch, runner, tmp_path, capsys):
    existing = "[ui]\ncopy_on_select = true\n"
    _configured(monkeypatch, runner, tmp_path, existing)
    capsys.readouterr()
    runner.calls.clear()
    monkeypatch.setattr(herdr_setup, "tomlkit", types.SimpleNamespace(parse=tomlkit.parse, dumps=_no_rewrite))
    assert _configured(monkeypatch, runner, tmp_path, existing) == existing
    assert ["/bin/herdr", "server", "reload-config"] not in runner.calls
    assert "[herdr] config:" not in capsys.readouterr().out


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
