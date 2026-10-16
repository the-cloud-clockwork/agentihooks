import json
import shlex
import subprocess
import sys

import pytest

from tests.swarm_node import profile_probe


def configured_home(tmp_path, command):
    home = tmp_path / "codex"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex/hooks.json").write_text(
        json.dumps({"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": command}]}]}})
    )
    return home


def test_offline_codex_profile_runs_rendered_hook_without_a_live_registry(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    hook = tmp_path / "hook.py"
    hook.write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "event = json.load(sys.stdin)\n"
        "assert event['hook_event_name'] == 'SessionStart'\n"
        "assert event['source'] == 'startup'\n"
        "home = Path(os.environ['HOME'])\n"
        "assert Path(os.environ['CODEX_HOME']) == home / '.codex'\n"
        "registry = home / '.agentihooks/active-sessions.json'\n"
        "assert not registry.exists()\n"
        "registry.parent.mkdir()\n"
        "registry.write_text(json.dumps({event['session_id']: {'cwd': event['cwd']}}))\n"
    )
    home = configured_home(tmp_path, f"{shlex.quote(sys.executable)} {shlex.quote(str(hook))}")

    result = profile_probe.codex_session_start(home)

    assert profile_probe.sessions(home) == {"worker-profile-codex": str(work)}
    assert result["registered_at"] == (home / ".agentihooks/active-sessions.json").stat().st_mtime
    assert result["elapsed_seconds"] > 0
    assert result["registration_to_exit_seconds"] >= 0
    assert profile_probe.harness("codex", tmp_path, tmp=home) == {"exit": 0, "output": ""}
    assert int((home / "codex-run.started").read_text()) > 0


def test_offline_codex_profile_rejects_a_failing_rendered_hook(tmp_path):
    home = configured_home(tmp_path, f"{shlex.quote(sys.executable)} -c 'raise SystemExit(7)'")

    with pytest.raises(subprocess.CalledProcessError) as failed:
        profile_probe.codex_session_start(home)

    assert failed.value.returncode == 7
    assert (home / "codex-run.exit").read_text() == "7"
    assert not (home / ".agentihooks/active-sessions.json").exists()


def test_offline_codex_profile_rejects_a_hook_that_does_not_register(tmp_path):
    home = configured_home(tmp_path, f"{shlex.quote(sys.executable)} -c 'pass'")

    with pytest.raises(FileNotFoundError):
        profile_probe.codex_session_start(home)


def test_offline_codex_profile_rejects_an_empty_session_start_group(tmp_path):
    home = configured_home(tmp_path, "unused")
    (home / ".codex/hooks.json").write_text(json.dumps({"hooks": {"SessionStart": []}}))

    with pytest.raises(AssertionError, match="no SessionStart hook"):
        profile_probe.codex_session_start(home)
