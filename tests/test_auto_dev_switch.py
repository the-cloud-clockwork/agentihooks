import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hooks.context import auto_dev_switch

pytestmark = pytest.mark.unit

_PROJECT_ROOT = Path(__file__).parent.parent


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo_on_main(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("hello\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")
    return repo


def _session_start(repo: Path, home: Path, setting: str | None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "AGENTIHOOKS_FORCE_DEV_BRANCH"}
    home.mkdir()
    (home / "deps.stamp").write_text(json.dumps({"ok_at": time.time()}))
    env.update(AGENTIHOOKS_HOME=str(home), AGENTIHOOKS_DISABLE_BYPASS_LOOKUP="1")
    if setting is not None:
        env["AGENTIHOOKS_FORCE_DEV_BRANCH"] = setting
    return subprocess.run(
        [sys.executable, "-m", "hooks"],
        input=json.dumps({"hook_event_name": "SessionStart", "session_id": "sz27-sid", "cwd": str(repo)}),
        capture_output=True,
        text=True,
        cwd=_PROJECT_ROOT,
        env=env,
    )


@pytest.mark.parametrize(("setting", "branch"), [(None, "main"), ("false", "main"), ("true", "dev")])
def test_session_start_switches_only_when_forced(repo_on_main, tmp_path, setting, branch):
    result = _session_start(repo_on_main, tmp_path / "home", setting)

    assert result.returncode == 0, result.stderr
    assert _git(repo_on_main, "rev-parse", "--abbrev-ref", "HEAD") == branch
    assert ("checkout:" in _git(repo_on_main, "reflog")) == (branch == "dev")
    assert ("[auto-dev-switch]" in result.stdout) == (branch == "dev")


def test_setting_defaults_to_false(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "AGENTIHOOKS_FORCE_DEV_BRANCH"}
    env["AGENTIHOOKS_HOME"] = str(tmp_path / "home")
    out = subprocess.run(
        [sys.executable, "-c", "import hooks.config as c; print(c.AGENTIHOOKS_FORCE_DEV_BRANCH)"],
        capture_output=True,
        text=True,
        cwd=_PROJECT_ROOT,
        env=env,
        check=True,
    ).stdout.strip()

    assert out == "False"


def test_off_runs_no_git(repo_on_main, monkeypatch):
    calls = []
    monkeypatch.setattr("hooks.config.AGENTIHOOKS_FORCE_DEV_BRANCH", False)
    monkeypatch.setattr(auto_dev_switch.subprocess, "run", lambda *a, **k: calls.append(a))

    assert auto_dev_switch.ensure_on_dev(str(repo_on_main)) == ""
    assert calls == []


def test_on_switches_existing_dev(repo_on_main, monkeypatch):
    _git(repo_on_main, "branch", "dev")
    monkeypatch.setattr("hooks.config.AGENTIHOOKS_FORCE_DEV_BRANCH", True)

    assert auto_dev_switch.ensure_on_dev(str(repo_on_main)) == "switched main → dev (existing branch)"
    assert _git(repo_on_main, "rev-parse", "--abbrev-ref", "HEAD") == "dev"
