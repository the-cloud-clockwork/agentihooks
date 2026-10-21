import json
import os
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
    assert result["registered_at"] == (home / ".agentihooks/active-sessions.json").stat().st_mtime_ns / 1e9
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


def registering_hook(tmp_path, then):
    hook = tmp_path / "hook.py"
    hook.write_text(
        "import json, os, subprocess, sys, time\n"
        "from pathlib import Path\n"
        "event = json.load(sys.stdin)\n"
        "home = Path(os.environ['HOME'])\n"
        "registry = home / '.agentihooks/active-sessions.json'\n"
        "registry.parent.mkdir()\n"
        "registry.write_text(json.dumps({event['session_id']: {'cwd': event['cwd']}}))\n"
        "(home / 'first').write_text(str(registry.stat().st_mtime_ns))\n" + then
    )
    return configured_home(tmp_path, f"{shlex.quote(sys.executable)} {shlex.quote(str(hook))}")


def test_offline_codex_profile_times_from_the_first_registry_write(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    home = registering_hook(tmp_path, "time.sleep(0.5)\nregistry.write_text(registry.read_text())\n")

    result = profile_probe.codex_session_start(home)

    first = int((home / "first").read_text())
    assert (home / ".agentihooks/active-sessions.json").stat().st_mtime_ns > first
    assert result["registered_at"] == first / 1e9


def test_offline_codex_profile_fails_on_a_hook_child_that_outlives_registration(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    home = registering_hook(
        tmp_path,
        "child = subprocess.Popen(['sleep', '30'], start_new_session=True)\n"
        "(home / 'child').write_text(str(child.pid))\n",
    )

    with pytest.raises(AssertionError, match="Codex hook children outlived registration") as failed:
        profile_probe.codex_session_start(home)

    child = int((home / "child").read_text())
    assert f"{child} sleep 30" in str(failed.value)
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)


def test_offline_codex_profile_names_a_surviving_child_of_a_failing_hook(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    home = registering_hook(
        tmp_path,
        "child = subprocess.Popen(['sleep', '30'], start_new_session=True)\n"
        "(home / 'child').write_text(str(child.pid))\n"
        "sys.exit(3)\n",
    )

    with pytest.raises(AssertionError, match="Codex hook children outlived registration") as failed:
        profile_probe.codex_session_start(home)

    assert isinstance(failed.value.__cause__, subprocess.CalledProcessError)
    assert f"{int((home / 'child').read_text())} sleep 30" in str(failed.value)


def test_offline_codex_profile_passes_a_hook_child_that_exits_with_its_hook(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    home = registering_hook(tmp_path, "subprocess.Popen(['sleep', '0.2'], start_new_session=True).wait()\n")

    result = profile_probe.codex_session_start(home)

    assert result["registered_at"] == int((home / "first").read_text()) / 1e9
