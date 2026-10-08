import os
import subprocess
from pathlib import Path

import pytest

from scripts import herdr_host, init_agent, install, operator_env, run_in_terminal
from scripts.swarm import cli as swarm_cli


def _home(tmp_path: Path, main: bool = True) -> Path:
    state = tmp_path / ".agentihooks"
    state.mkdir()
    if main:
        (state / ".env").write_text("MAIN_ONLY=main\nSHARED=main\n")
    (state / "langfuse.env").write_text("export SECONDARY_ONLY=secondary\nSHARED=secondary\n")
    (state / "quota.env").write_text("SHARED=quota\n")
    (state / ".hidden.env").write_text("HIDDEN=1\n")
    (state / "notes.txt").write_text("STRAY=1\n")
    (tmp_path / ".env").write_text("PERSONAL_ONLY='personal'\nSHARED=personal\n")
    return state


def test_the_list_is_main_then_sorted_companions_then_the_home_file(tmp_path):
    state = _home(tmp_path)
    assert operator_env.files({"HOME": str(tmp_path)}) == [
        state / ".env",
        state / "langfuse.env",
        state / "quota.env",
        tmp_path / ".env",
    ]


def test_companions_load_only_beside_a_main_file(tmp_path):
    _home(tmp_path, main=False)
    assert operator_env.files({"HOME": str(tmp_path)}) == [tmp_path / ".env"]


def test_the_agentihooks_home_setting_moves_the_folder(tmp_path):
    state = tmp_path / "elsewhere"
    state.mkdir()
    (state / ".env").write_text("A=1\n")
    assert operator_env.files({"HOME": str(tmp_path), "AGENTIHOOKS_HOME": str(state)}) == [state / ".env"]


def test_nothing_to_load_runs_no_shell(tmp_path, monkeypatch):
    monkeypatch.setattr(operator_env.subprocess, "run", lambda *a, **k: pytest.fail("shell ran"))
    assert operator_env.values({"HOME": str(tmp_path)}) == {}
    assert operator_env.source_line({"HOME": str(tmp_path)}) == ""


def test_values_follow_the_shell_where_a_later_file_wins(tmp_path):
    _home(tmp_path)
    loaded = operator_env.values({"HOME": str(tmp_path), "PATH": os.environ["PATH"]})
    assert loaded["MAIN_ONLY"] == "main"
    assert loaded["SECONDARY_ONLY"] == "secondary"
    assert loaded["PERSONAL_ONLY"] == "personal"
    assert loaded["SHARED"] == "personal"
    assert "HIDDEN" not in loaded and "STRAY" not in loaded
    assert not operator_env.SHELL_NAMES & loaded.keys()


def test_accounts_are_the_interactive_shell_account_variables_only(tmp_path):
    (tmp_path / ".profile").write_text(
        "echo AH_CC_TOKEN_noise=printed\n"
        "export AH_CC_TOKEN_alpha=first\n"
        "export AH_CC_TOKEN_beta='second value'\n"
        "export UNRELATED=other\n"
    )
    environ = {"HOME": str(tmp_path), "PATH": os.environ["PATH"], "AH_CC_TOKEN_beta": "stale"}
    assert operator_env.accounts(environ) == {"AH_CC_TOKEN_alpha": "first", "AH_CC_TOKEN_beta": "second value"}


def test_accounts_are_empty_when_the_shell_exports_none(tmp_path):
    assert operator_env.accounts({"HOME": str(tmp_path), "PATH": os.environ["PATH"]}) == {}


def test_the_files_see_home_path_and_agentihooks_home_and_nothing_else(tmp_path, monkeypatch):
    state = tmp_path / "state"
    tools = tmp_path / "tools"
    state.mkdir()
    tools.mkdir()
    (tools / "probe-tool").write_text("#!/bin/sh\necho found\n")
    (tools / "probe-tool").chmod(0o755)
    (state / ".env").write_text(
        'REF="$HOME/x"\nTOOL="$(probe-tool)"\nAH="${AGENTIHOOKS_HOME:-unset}"\nLEAK="${OPERATOR_ENV_LEAK:-none}"\n'
    )
    monkeypatch.setenv("OPERATOR_ENV_LEAK", "caller")
    environ = {"HOME": str(tmp_path), "PATH": f"{tools}:/usr/bin:/bin", "AGENTIHOOKS_HOME": str(state)}
    loaded = operator_env.values({**environ, "OPERATOR_ENV_LEAK": "caller"})
    assert loaded["REF"] == f"{tmp_path}/x"
    assert loaded["TOOL"] == "found"
    assert loaded["AH"] == str(state)
    assert loaded["LEAK"] == "none"


def test_values_keep_equals_signs_and_undecodable_bytes(tmp_path):
    state = tmp_path / ".agentihooks"
    state.mkdir()
    (state / ".env").write_bytes(b"URL='a=b=c'\nRAW=caf\xff\n")
    loaded = operator_env.values({"HOME": str(tmp_path), "PATH": os.environ["PATH"]})
    assert loaded["URL"] == "a=b=c"
    assert loaded["RAW"] == "caf\udcff"


def test_fill_adds_only_names_the_caller_lacks(tmp_path):
    _home(tmp_path)
    environ = {"HOME": str(tmp_path), "PATH": os.environ["PATH"], "SHARED": "caller"}
    added = operator_env.fill(environ)
    assert environ["SHARED"] == "caller"
    assert environ["SECONDARY_ONLY"] == "secondary"
    assert "SHARED" not in added and "SECONDARY_ONLY" in added


def test_the_list_matches_the_shell_block_init_writes(tmp_path, monkeypatch):
    state = _home(tmp_path)
    bashrc = tmp_path / ".bashrc"
    monkeypatch.setattr(install, "_BASHRC", bashrc)
    monkeypatch.setattr(install, "_ENV_FILE_DST", state / ".env")
    install._update_bashrc_block()
    shell = subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", f". {bashrc} >/dev/null 2>&1; env -0"],
        env={"HOME": str(tmp_path), "PATH": os.environ["PATH"]},
        capture_output=True,
        check=True,
    )
    from_shell = dict(item.decode().partition("=")[::2] for item in shell.stdout.split(b"\0") if item)
    loaded = operator_env.values({"HOME": str(tmp_path), "PATH": os.environ["PATH"]})
    for name in ("MAIN_ONLY", "SECONDARY_ONLY", "PERSONAL_ONLY", "SHARED", "HIDDEN", "STRAY"):
        assert loaded.get(name) == from_shell.get(name)


def test_a_name_only_in_a_secondary_file_reaches_the_render(tmp_path, monkeypatch, capsys):
    from scripts import select_profile

    _home(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    seen = {}

    def prepare(profile, agent, model, effort, flags, environ, overlays=()):
        seen.update(environ)
        return {}, flags

    monkeypatch.setattr(select_profile, "prepare", prepare)
    monkeypatch.setattr(herdr_host, "binary", lambda: None)
    monkeypatch.setattr("scripts.profile_telemetry.installed_langfuse_env", lambda target: {})
    monkeypatch.setattr(
        init_agent,
        "_launch_command",
        lambda launcher, directory, title, environ: ("linux", ["/usr/bin/terminal", str(launcher)]),
    )
    environ = {
        "HOME": str(tmp_path),
        "PATH": os.environ["PATH"],
        "XDG_RUNTIME_DIR": str(tmp_path / "runtime"),
        "SHELL": "/bin/bash",
    }
    rc = init_agent.main(["--dir", str(project), "--name", "env-test", "--profile", "engineer", "--dry-run"], environ)
    assert rc == 0, capsys.readouterr().err
    assert seen["SECONDARY_ONLY"] == "secondary"
    assert seen["PERSONAL_ONLY"] == "personal"


def test_the_tick_fills_its_environment_before_any_spawn(tmp_path, monkeypatch):
    _home(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in ("MAIN_ONLY", "SECONDARY_ONLY", "PERSONAL_ONLY", "SHARED"):
        monkeypatch.delenv(name, raising=False)
    seen = []

    class Store:
        def slugs(self):
            seen.append(os.environ.get("SECONDARY_ONLY"))
            return []

    swarm_cli.cmd_tick(Store(), None)
    assert seen == ["secondary"]
    assert os.environ["PERSONAL_ONLY"] == "personal"


def _launcher(tmp_path, environ):
    env = {"XDG_RUNTIME_DIR": str(tmp_path / "run"), "SHELL": "/bin/bash", **environ}
    launcher, _ = init_agent._write_launcher(tmp_path, "grace", "", [], env, init_agent.AgentSpec())
    return launcher.read_text()


def test_the_launcher_waits_its_grace_before_the_agent_command(tmp_path):
    lines = _launcher(tmp_path, {"HOME": str(tmp_path)}).splitlines()
    assert init_agent.LAUNCH_GRACE_S == 3
    grace = lines.index("sleep 3")
    assert any(line.endswith(".started") for line in lines[:grace])
    assert "claude" in lines[grace + 1] and lines[grace + 2].startswith("rm -f ")


def test_the_launcher_sources_the_shared_list_before_its_own_exports(tmp_path):
    _home(tmp_path)
    text = _launcher(tmp_path, {"HOME": str(tmp_path)})
    line = operator_env.source_line({"HOME": str(tmp_path)})
    assert line and line in text
    assert str(tmp_path / ".env") in line and str(tmp_path / ".agentihooks" / "langfuse.env") in line
    assert text.index(line) < text.index("export AGENTIHOOKS_AGENT_NAME=")


def test_the_launcher_source_line_loads_what_the_shell_loads(tmp_path):
    _home(tmp_path)
    line = operator_env.source_line({"HOME": str(tmp_path)})
    done = subprocess.run(
        [
            "bash",
            "--noprofile",
            "--norc",
            "-c",
            f'{line}printf \'%s|%s|%s|%s\' "$SECONDARY_ONLY" "$SHARED" "$#" "$(env | grep -c \'^PERSONAL_ONLY=\')"',
        ],
        env={"HOME": str(tmp_path), "PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        check=True,
    )
    assert done.stdout == "secondary|personal|0|1"


@pytest.fixture
def herdr(monkeypatch):
    calls = []
    monkeypatch.setattr(herdr_host, "binary", lambda: "/bin/herdr")
    monkeypatch.setattr(herdr_host, "ensure_server", lambda environ: False)
    monkeypatch.setattr(
        herdr_host,
        "open_pane",
        lambda *a: calls.append("open") or herdr_host.Placement("w1", "w1:t2", "w1:p3"),
    )
    monkeypatch.setattr(herdr_host, "_cli", lambda args, environ: calls.append((*args, environ.get("HOME"))) or {})
    return calls


def test_run_in_terminal_waits_its_grace_in_a_herdr_pane(herdr, tmp_path, capsys):
    assert run_in_terminal.main(["--dir", str(tmp_path), "--", "npm", "test"], {"HOME": str(tmp_path)}) == 0
    assert ("pane", "run", "w1:p3", f"sleep {init_agent.LAUNCH_GRACE_S} && npm test", str(tmp_path)) in herdr
    assert capsys.readouterr().out.splitlines()[:2] == ["host=herdr", f"directory={tmp_path}"]


def test_run_in_terminal_waits_its_grace_in_a_native_terminal(monkeypatch, tmp_path):
    ran = []
    monkeypatch.setattr(herdr_host, "binary", lambda: None)
    monkeypatch.setattr(
        run_in_terminal.subprocess, "run", lambda argv, **k: ran.append(argv) or subprocess.CompletedProcess(argv, 0)
    )
    assert run_in_terminal.main(["--dir", str(tmp_path), "--", "make"], {"HOME": str(tmp_path)}) == 0
    assert ran[0][-1] == f"sleep {init_agent.LAUNCH_GRACE_S} && make"


def test_a_plain_shell_tab_has_no_command_to_delay(herdr, tmp_path):
    assert run_in_terminal.main(["--dir", str(tmp_path)], {"HOME": str(tmp_path)}) == 0
    assert not any(call[:2] == ("pane", "run") for call in herdr if isinstance(call, tuple))
