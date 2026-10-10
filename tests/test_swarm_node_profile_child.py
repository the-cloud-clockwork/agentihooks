import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "swarm_v2" / "worker_home.py"
FIXTURES = ROOT / "docker" / "swarm-node" / "fixtures" / "profiles"
WORKSTATION = "/home/operator/dev/tcc-ecosystem/.venv/bin/python"


def bootstrap(volume: Path, templates: Path, attempt: str, claude: str) -> subprocess.CompletedProcess:
    argv = [
        sys.executable,
        str(SCRIPT),
        "bootstrap",
        f"--root={volume}",
        f"--attempt={attempt}",
        f"--templates={templates}",
        f"--interpreter={sys.executable}",
        f"--uid={os.geteuid()}",
        f"--gid={os.getegid()}",
        f"--profile=claude={claude}",
        "--profile=codex=fixture-codex",
        "--account=claude=AH_CC_TOKEN_POOL_A",
        "--endpoint=BRAIN_URL=https://brain.swarm.svc",
    ]
    env = {"PATH": os.environ["PATH"], "PYTHONPATH": str(ROOT), "HOME": str(volume)}
    return subprocess.run(argv, env=env, cwd=volume, capture_output=True, text=True, timeout=300)


def test_child_processes_render_both_targets_with_the_requested_interpreter(tmp_path):
    templates, volume = tmp_path / "templates", tmp_path / "volume"
    shutil.copytree(FIXTURES, templates)
    volume.mkdir()
    done = bootstrap(volume, templates, "attempt-1", "fixture-claude")
    assert done.returncode == 0, done.stderr
    record = json.loads(done.stdout)
    homes = volume / "attempt-1" / "homes"
    settings = json.loads((homes / "claude" / ".claude" / "settings.json").read_text())
    config = tomllib.loads((homes / "codex" / ".codex" / "config.toml").read_text())
    commands = [h["command"] for groups in settings["hooks"].values() for g in groups for h in g["hooks"]]
    assert record["reused"] is False and record["accounts"] == {"claude": "AH_CC_TOKEN_POOL_A"}
    assert all(sys.executable in command for command in commands)
    assert settings["env"]["BRAIN_URL"] == "https://brain.swarm.svc"
    assert config["mcp_servers"]["agentihooks"]["command"] == sys.executable
    assert sorted(config["mcp_servers"]) == ["agentihooks", "fixture-codex-mcp"]
    assert (volume / "attempt-1" / "run" / "render-claude.log").is_file()


def test_child_process_render_of_a_workstation_profile_is_refused(tmp_path):
    templates, volume = tmp_path / "templates", tmp_path / "volume"
    shutil.copytree(FIXTURES, templates)
    volume.mkdir()
    done = bootstrap(volume, templates, "attempt-1", "fixture-workstation")
    assert done.returncode == 1
    assert done.stderr == f"ERROR: claude setting hooks leaves the execution root: {WORKSTATION}\n"
    assert list(volume.iterdir()) == []
