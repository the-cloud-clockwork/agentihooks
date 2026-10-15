import subprocess
import sys
from pathlib import Path

import pytest

from scripts import gh_config, init_agent

REPO = Path(__file__).resolve().parents[1]

FAKE_GH = """#!/usr/bin/env bash
set -euo pipefail
if [[ -z "${GH_CONFIG_DIR:-}" && -n "${XDG_CONFIG_HOME:-}" && ! -d "$XDG_CONFIG_HOME/gh" ]]; then
  mv "$HOME/.config/gh" "$XDG_CONFIG_HOME/gh"
fi
"""


def _isolated(tmp_path):
    home = tmp_path / "operator"
    (home / ".config" / "gh").mkdir(parents=True)
    (home / ".config" / "gh" / "hosts.yml").write_text("github.com: {}\n")
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "gh").write_text(FAKE_GH)
    (tools / "gh").chmod(0o755)
    isolated = tmp_path / "proof" / "config"
    isolated.mkdir(parents=True)
    return home, {"HOME": str(home), "XDG_CONFIG_HOME": str(isolated), "PATH": f"{tools}:/usr/bin:/bin"}


def _home_login_kept(home, environ, command):
    subprocess.run(command, env=environ, check=True, cwd=REPO)
    return (home / ".config" / "gh" / "hosts.yml").is_file()


def test_an_unpinned_isolated_gh_moves_the_home_login(tmp_path):
    home, environ = _isolated(tmp_path)
    assert not _home_login_kept(home, environ, ["gh"])


def test_a_pinned_environment_keeps_the_home_login(tmp_path):
    home, environ = _isolated(tmp_path)
    gh_config.pin(environ)
    assert environ["GH_CONFIG_DIR"] == str(home / ".config" / "gh")
    assert _home_login_kept(home, environ, ["gh"])


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({"HOME": "/h", "XDG_CONFIG_HOME": "/h/.config"}, None),
        ({"HOME": "/h"}, None),
        ({"HOME": "/h", "XDG_CONFIG_HOME": "/x", "GH_CONFIG_DIR": "/g"}, "/g"),
        ({"HOME": "/h", "XDG_CONFIG_HOME": "/x", "GH_CONFIG_DIR": ""}, "/h/.config/gh"),
    ],
)
def test_the_pin_only_fills_an_isolated_unpinned_environment(environ, expected):
    gh_config.pin(environ)
    assert environ.get("GH_CONFIG_DIR") == expected


@pytest.mark.parametrize(
    "start",
    [
        "import hooks.config",
        "sys.argv = ['agentihooks', 'version']\nfrom scripts.install import main\nwith contextlib.suppress(SystemExit):\n    main()",
    ],
)
def test_an_agentihooks_process_in_an_isolated_session_keeps_the_home_login(tmp_path, start):
    home, environ = _isolated(tmp_path)
    environ["PYTHONPATH"] = str(REPO)
    code = f"import contextlib, subprocess, sys\n{start}\nsubprocess.run(['gh'], check=True)\n"
    assert _home_login_kept(home, environ, [sys.executable, "-c", code])


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_an_isolated_launcher_keeps_the_home_login(monkeypatch, tmp_path, agent):
    monkeypatch.setattr("scripts.profile_telemetry.installed_langfuse_env", lambda target: {})
    home, environ = _isolated(tmp_path)
    spec = init_agent.AgentSpec(agent=agent)
    launcher, _ = init_agent._write_launcher(tmp_path, "proof", "", [], {"XDG_RUNTIME_DIR": str(tmp_path)}, spec)
    pins = [line for line in launcher.read_text().splitlines() if "GH_CONFIG_DIR" in line]
    assert pins == [gh_config.SHELL_PIN.strip()]
    assert _home_login_kept(home, environ, ["bash", "-c", f"{pins[0]}\ngh"])
