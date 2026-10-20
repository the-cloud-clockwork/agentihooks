from __future__ import annotations

import contextlib
import inspect
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import Mock

import install
import pytest

from hooks.lifecycle import deps_kick
from scripts import deps_preflight, tool_deps


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "ah-home"
    home.mkdir()
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(home))
    manifest = tmp_path / "deps.json"
    monkeypatch.setattr(deps_preflight, "manifest_path", lambda: manifest)
    monkeypatch.setattr(tool_deps, "SOURCE", tmp_path)
    marker = tmp_path / "installed"
    calls = tmp_path / "check-calls"

    def write(deps: list[dict]) -> None:
        manifest.write_text(json.dumps({"deps": deps}))

    return {"home": home, "manifest": manifest, "marker": marker, "calls": calls, "write": write}


def _tool(env) -> dict:
    marker, calls = env["marker"], env["calls"]
    return {
        "id": "tool",
        "kind": "uv-tool",
        "check": ["sh", "-c", f"echo x >> {calls}; test -e {marker}"],
        "install": ["touch", str(marker)],
    }


def test_missing_installable_dep_is_installed_and_logged(env):
    env["write"]([_tool(env)])
    assert deps_preflight.ensure(quiet=True) == 0
    assert env["marker"].exists()
    logged = [json.loads(line) for line in deps_preflight.installs_log().read_text().splitlines()]
    assert logged[-1]["id"] == "tool" and logged[-1]["ok"] is True


def test_fresh_stamp_skips_the_checks(env):
    env["write"]([_tool(env)])
    deps_preflight.ensure(quiet=True)
    before = env["calls"].read_text().count("x")
    assert deps_preflight.ensure(quiet=True) == 0
    assert env["calls"].read_text().count("x") == before


def test_manifest_change_invalidates_the_stamp(env):
    env["write"]([_tool(env)])
    deps_preflight.ensure(quiet=True)
    before = env["calls"].read_text().count("x")
    env["write"]([{**_tool(env), "hint": "changed"}])
    deps_preflight.ensure(quiet=True)
    assert env["calls"].read_text().count("x") > before


def test_system_dep_is_reported_never_installed(env, capsys):
    env["write"]([{"id": "gh", "kind": "system", "check": ["false"], "hint": "sudo apt install gh"}])
    assert deps_preflight.ensure(quiet=True) == deps_preflight.EXIT_MISSING_SYSTEM
    assert "sudo apt install gh" in capsys.readouterr().err
    assert not deps_preflight.installs_log().exists()


def test_absent_dep_is_removed(env):
    env["marker"].touch()
    env["write"]([{**_tool(env), "state": "absent", "install": ["rm", "-f", str(env["marker"])]}])
    assert deps_preflight.ensure(quiet=True) == 0
    assert not env["marker"].exists()


def test_unknown_kind_is_rejected():
    with pytest.raises(ValueError, match="apt"):
        deps_preflight.parse({"deps": [{"id": "x", "kind": "apt", "check": ["true"]}]})


def test_check_names_a_declared_dependency_missing_from_the_tool_environment(env, tmp_path, capsys):
    env["write"]([])
    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndependencies = ["pytest>=7", "agentihooks-absent-dist-for-test>=1"]\n'
    )
    assert deps_preflight.main(["check"]) == 1
    assert capsys.readouterr().out == (
        "missing: agentihooks-absent-dist-for-test>=1 (python) — declared in pyproject.toml but missing from the "
        f"agentihooks tool environment; install it with uv pip install --python {sys.executable} "
        "'agentihooks-absent-dist-for-test>=1'\ndeps: 1 unsatisfied\n"
    )


def test_check_passes_when_every_declared_dependency_is_installed(env, tmp_path, capsys):
    env["write"]([])
    (tmp_path / "pyproject.toml").write_text('[project]\ndependencies = ["pytest>=7"]\n')
    assert deps_preflight.main(["check"]) == 0
    assert capsys.readouterr().out == "deps: all satisfied\n"


def test_ensure_installs_a_declared_dependency_missing_from_the_tool_environment(env, tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(tool_deps, "call", lambda argv: calls.append(argv) or "")
    env["write"]([])
    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndependencies = ["pytest>=7", "agentihooks-absent-dist-for-test>=1", "agentihooks-absent-b"]\n'
    )
    assert deps_preflight.ensure() == 0
    assert calls == [
        [
            "uv",
            "pip",
            "install",
            "--python",
            sys.executable,
            "agentihooks-absent-dist-for-test>=1",
            "agentihooks-absent-b",
        ]
    ]
    assert capsys.readouterr().err == (
        "[agentihooks] deps: installing agentihooks-absent-dist-for-test>=1, agentihooks-absent-b "
        "into the agentihooks tool environment\n"
    )


def test_ensure_reports_a_failed_declared_install_and_keeps_going(env, tmp_path, monkeypatch, capsys):
    def fail(argv):
        raise RuntimeError("uv pip install failed: no network")

    monkeypatch.setattr(tool_deps, "call", fail)
    (tmp_path / "pyproject.toml").write_text('[project]\ndependencies = ["agentihooks-absent-dist-for-test>=1"]\n')
    env["write"]([_tool(env)])
    assert deps_preflight.ensure() == 0
    assert env["marker"].exists()
    assert "[agentihooks] deps: uv pip install failed: no network\n" in capsys.readouterr().err


def test_ensure_quiet_hides_a_failed_declared_install(env, tmp_path, monkeypatch, capsys):
    def fail(argv):
        raise RuntimeError("uv pip install failed: no network")

    monkeypatch.setattr(tool_deps, "call", fail)
    env["write"]([])
    (tmp_path / "pyproject.toml").write_text('[project]\ndependencies = ["agentihooks-absent-dist-for-test>=1"]\n')
    assert deps_preflight.ensure(quiet=True) == 0
    assert capsys.readouterr().err == ""


def test_ensure_with_every_declared_dependency_installed_runs_no_install(env, tmp_path, monkeypatch):
    monkeypatch.setattr(tool_deps, "call", lambda argv: pytest.fail(f"ran {argv}"))
    env["write"]([])
    (tmp_path / "pyproject.toml").write_text('[project]\ndependencies = ["pytest>=7"]\n')
    assert deps_preflight.ensure(quiet=True) == 0


def test_sync_pulls_the_tool_source_and_reports_what_it_installed(env, monkeypatch, capsys):
    pulled = []
    monkeypatch.setattr(tool_deps, "call", lambda argv: "dev" if "rev-parse" in argv else "")
    monkeypatch.setattr(
        tool_deps, "pull", lambda repo, call, python: pulled.append((repo, call, python)) or ["croniter>=6", "rich"]
    )
    assert deps_preflight.main(["sync"]) == 0
    assert pulled == [(str(tool_deps.SOURCE), tool_deps.call, sys.executable)]
    assert capsys.readouterr().out == "deps sync: installed croniter>=6, rich into the agentihooks tool environment\n"


def test_an_unknown_deps_action_is_refused():
    with pytest.raises(SystemExit):
        deps_preflight.main(["bogus"])


def test_a_missing_declared_dependency_carries_no_check_command(env, tmp_path):
    env["write"]([])
    (tmp_path / "pyproject.toml").write_text('[project]\ndependencies = ["agentihooks-absent-dist-for-test>=1"]\n')
    assert [(dep.id, dep.check) for dep in deps_preflight.unsatisfied()] == [
        ("agentihooks-absent-dist-for-test>=1", ())
    ]


def test_sync_with_unchanged_dependencies_says_so(env, monkeypatch, capsys):
    monkeypatch.setattr(tool_deps, "call", lambda argv: "dev")
    monkeypatch.setattr(tool_deps, "pull", lambda repo, call, python: [])
    assert deps_preflight.main(["sync"]) == 0
    assert capsys.readouterr().out == "deps sync: pulled dev; declared dependencies unchanged\n"


def test_sync_refuses_a_source_checkout_that_is_not_on_dev(env, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(tool_deps, "call", lambda argv: calls.append(argv) or "feature")
    monkeypatch.setattr(tool_deps, "pull", lambda *a: pytest.fail("pulled a checkout off dev"))
    assert deps_preflight.main(["sync"]) == 1
    assert calls == [["git", "-C", str(tool_deps.SOURCE), "rev-parse", "--abbrev-ref", "HEAD"]]
    assert capsys.readouterr().err == f"deps sync: {tool_deps.SOURCE} is on feature, not dev\n"


def test_sync_reports_a_failed_pull(env, monkeypatch, capsys):
    def fail(argv):
        if "rev-parse" in argv:
            return "dev"
        raise RuntimeError("not a fast forward")

    monkeypatch.setattr(tool_deps, "call", fail)
    assert deps_preflight.main(["sync"]) == 1
    assert capsys.readouterr().err == "deps sync: not a fast forward\n"


def test_fleet_plugins_are_the_present_claude_plugins(env, tmp_path):
    plugin = {"kind": "claude-plugin", "check": ["true"], "install": ["true"]}
    env["write"](
        [
            {**plugin, "id": "kept@m"},
            {**plugin, "id": "gone@m", "state": "absent"},
            {**_tool(env), "id": "kept-tool"},
            {**plugin, "id": "also@m"},
        ]
    )
    assert deps_preflight.fleet_plugins(tmp_path) == ["kept@m", "also@m"]
    assert deps_preflight.fleet_plugins(tmp_path / "no-bundle") == []
    assert deps_preflight.fleet_plugins(None) == []


def test_launch_runs_the_preflight_before_taking_the_route_lock():
    source = inspect.getsource(install.cmd_claude)
    assert source.index("deps_ensure()") < source.index("route_lock_path =")


def test_session_start_kick_spawns_only_when_stale(tmp_path, monkeypatch):
    spawned = []
    monkeypatch.setattr(deps_kick.shutil, "which", lambda name: "/usr/bin/agentihooks")
    monkeypatch.setattr(deps_kick.subprocess, "Popen", lambda argv, **kw: spawned.append(argv) or Mock())
    monkeypatch.setattr(deps_kick.os, "killpg", lambda pid, sig: None)
    assert deps_kick.kick(home=tmp_path) is True
    (tmp_path / "deps.stamp").write_text(json.dumps({"ok_at": time.time()}))
    assert deps_kick.kick(home=Path(tmp_path)) is False
    assert spawned == [["/usr/bin/agentihooks", "deps", "ensure", "--quiet"]]


def _live_group_members(pgid: int) -> list[str]:
    members = []
    for stat in Path("/proc").glob("[0-9]*/stat"):
        with contextlib.suppress(OSError):
            fields = stat.read_text().rsplit(")", 1)[1].split()
            if int(fields[2]) == pgid and fields[0] != "Z":
                members.append(stat.parent.name)
    return members


@pytest.mark.wall_clock
@pytest.mark.parametrize("script", ["sleep 30 &\nwait\n", "sleep 30 &\nexit 0\n"], ids=["slow", "leaves-a-child"])
def test_session_start_kick_leaves_no_check_process_behind(tmp_path, monkeypatch, script):
    exe = tmp_path / "agentihooks"
    exe.write_text(f"#!/bin/sh\n{script}")
    exe.chmod(0o755)
    started = []
    popen = subprocess.Popen
    monkeypatch.setattr(deps_kick.shutil, "which", lambda name: str(exe))
    monkeypatch.setattr(deps_kick.subprocess, "Popen", lambda *a, **kw: started.append(popen(*a, **kw)) or started[-1])
    began = time.monotonic()
    try:
        assert deps_kick.kick(home=tmp_path) is True
        elapsed = time.monotonic() - began
        deadline = time.monotonic() + 2
        while (survivors := _live_group_members(started[0].pid)) and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        if started:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(started[0].pid, signal.SIGKILL)
            started[0].wait()
    assert elapsed < 5
    assert survivors == [], "the dependency check's process group outlived the session start kick"


def test_session_start_kick_waits_for_a_fast_check(tmp_path, monkeypatch):
    exe = tmp_path / "agentihooks"
    exe.write_text("#!/bin/sh\nsleep 0.1\nexit 3\n")
    exe.chmod(0o755)
    started = []
    popen = subprocess.Popen
    monkeypatch.setattr(deps_kick.shutil, "which", lambda name: str(exe))
    monkeypatch.setattr(deps_kick.subprocess, "Popen", lambda *a, **kw: started.append(popen(*a, **kw)) or started[-1])
    assert deps_kick.kick(home=tmp_path) is True
    assert started[0].returncode == 3


class _OutlivingCheck:
    pid = 4242

    def __init__(self):
        self.waits = []

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if len(self.waits) == 1:
            raise subprocess.TimeoutExpired("agentihooks", timeout)
        return -signal.SIGKILL


def test_session_start_kick_reaps_a_check_that_outlives_the_bounded_wait(tmp_path, monkeypatch):
    check = _OutlivingCheck()
    killed = []
    monkeypatch.setattr(deps_kick.shutil, "which", lambda name: "/usr/bin/agentihooks")
    monkeypatch.setattr(deps_kick.subprocess, "Popen", lambda argv, **kw: check)
    monkeypatch.setattr(deps_kick.os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    assert deps_kick.kick(home=tmp_path) is True
    assert check.waits == [deps_kick.WAIT_SECONDS, None]
    assert deps_kick.WAIT_SECONDS == 1.0
    assert killed == [(4242, signal.SIGKILL)]
