from __future__ import annotations

import inspect
import json
import time
from pathlib import Path

import install
import pytest

from hooks.lifecycle import deps_kick
from scripts import deps_preflight


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "ah-home"
    home.mkdir()
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(home))
    manifest = tmp_path / "deps.json"
    monkeypatch.setattr(deps_preflight, "manifest_path", lambda: manifest)
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
    monkeypatch.setattr(deps_kick.subprocess, "Popen", lambda argv, **kw: spawned.append(argv))
    assert deps_kick.kick(home=tmp_path) is True
    (tmp_path / "deps.stamp").write_text(json.dumps({"ok_at": time.time()}))
    assert deps_kick.kick(home=Path(tmp_path)) is False
    assert spawned == [["/usr/bin/agentihooks", "deps", "ensure", "--quiet"]]
