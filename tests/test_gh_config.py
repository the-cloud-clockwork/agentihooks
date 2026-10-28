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


@pytest.mark.parametrize(("actions", "sparse"), [("true", True), ("true", False), ("false", False), (None, False)])
def test_package_bootstrap_keeps_workflow_checkouts_independent_of_local_auditing(tmp_path, actions, sparse):
    home, environ = _isolated(tmp_path)
    checkout = tmp_path / "checkout"
    package = checkout / "scripts"
    package.mkdir(parents=True)
    files = ["__init__.py", "gh_config.py"]
    if not sparse:
        files.append("gh_audit.py")
    for name in files:
        (package / name).write_text((REPO / "scripts" / name).read_text())
    if actions is not None:
        environ["GITHUB_ACTIONS"] = actions
    code = (
        "import subprocess, sys\n"
        "hooks = []\n"
        "sys.addaudithook = hooks.append\n"
        f"sys.path.insert(0, {str(checkout)!r})\n"
        "import scripts\n"
        f"assert scripts.__file__ == {str(package / '__init__.py')!r}\n"
        f"assert len(hooks) == {int(actions != 'true')}\n"
        f"assert ('scripts.gh_audit' in sys.modules) is {actions != 'true'}\n"
    )
    if actions == "true":
        code += "subprocess.run(['gh', 'run', 'cancel', '1'], check=True)\n"
    result = subprocess.run([sys.executable, "-c", code], cwd=checkout, env=environ, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_a_hook_hands_the_pin_to_the_session_shell_once(tmp_path):
    home, environ = _isolated(tmp_path)
    session_env = tmp_path / "session.env"
    hook = {**environ, "PYTHONPATH": str(REPO), "CLAUDE_ENV_FILE": str(session_env)}
    for _ in range(2):
        subprocess.run([sys.executable, "-c", "import hooks.config"], env=hook, check=True, cwd=REPO)
    assert session_env.read_text() == f"export GH_CONFIG_DIR={home / '.config' / 'gh'}\n"
    assert _home_login_kept(home, environ, ["bash", "-c", f"source {session_env}\ngh"])


def test_a_session_without_a_pin_writes_nothing(tmp_path):
    session_env = tmp_path / "session.env"
    gh_config.persist({"HOME": str(tmp_path), "CLAUDE_ENV_FILE": str(session_env)})
    gh_config.persist({"HOME": str(tmp_path), "GH_CONFIG_DIR": "/g"})
    assert not session_env.exists()


def test_the_pin_starts_its_own_line_in_a_session_env_file(tmp_path):
    session_env = tmp_path / "session.env"
    session_env.write_text("export A=1")
    gh_config.persist({"CLAUDE_ENV_FILE": str(session_env), "GH_CONFIG_DIR": "/g"})
    assert session_env.read_text() == "export A=1\nexport GH_CONFIG_DIR=/g\n"


def test_an_unwritable_session_env_file_never_breaks_a_hook(tmp_path):
    gh_config.persist({"CLAUDE_ENV_FILE": str(tmp_path / "gone" / "session.env"), "GH_CONFIG_DIR": "/g"})
    assert not (tmp_path / "gone").exists()


def test_the_pin_is_the_whole_of_a_new_session_env_file(tmp_path):
    session_env = tmp_path / "session.env"
    gh_config.persist({"CLAUDE_ENV_FILE": str(session_env), "GH_CONFIG_DIR": "/g"})
    assert session_env.read_text() == "export GH_CONFIG_DIR=/g\n"


def test_the_pin_follows_a_complete_line_directly(tmp_path):
    session_env = tmp_path / "session.env"
    session_env.write_text("export A=1\n")
    gh_config.persist({"CLAUDE_ENV_FILE": str(session_env), "GH_CONFIG_DIR": "/g"})
    assert session_env.read_text() == "export A=1\nexport GH_CONFIG_DIR=/g\n"


def test_an_undecodable_session_env_file_never_breaks_a_hook(tmp_path):
    session_env = tmp_path / "session.env"
    session_env.write_bytes(b"\xff\xfe\xfa")
    gh_config.persist({"CLAUDE_ENV_FILE": str(session_env), "GH_CONFIG_DIR": "/g"})
    assert session_env.read_bytes() == b"\xff\xfe\xfa"


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_an_isolated_launcher_keeps_the_home_login(monkeypatch, tmp_path, agent):
    monkeypatch.setattr("scripts.profile_telemetry.installed_langfuse_env", lambda target: {})
    home, environ = _isolated(tmp_path)
    spec = init_agent.AgentSpec(agent=agent)
    launcher, _ = init_agent._write_launcher(tmp_path, "proof", "", [], {"XDG_RUNTIME_DIR": str(tmp_path)}, spec)
    pins = [line for line in launcher.read_text().splitlines() if "GH_CONFIG_DIR" in line]
    assert pins == [gh_config.SHELL_PIN.strip()]
    assert _home_login_kept(home, environ, ["bash", "-c", f"{pins[0]}\ngh"])
