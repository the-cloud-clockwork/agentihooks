import json
import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
START = ROOT / "deploy/worker/start.sh"


def startup(tmp_path, *, joined=False, refused=False):
    tools = tmp_path / "bin"
    tools.mkdir()
    log = tmp_path / "commands"
    cli = tools / "agentihooks"
    cli.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        'printf "%s\\n" "$*" >> "$COMMAND_LOG"\n'
        'if [[ "$*" == "hive join "* ]]; then\n'
        '  [[ "$REFUSE_JOIN" == no ]] || exit 9\n'
        '  touch "$AGENTIHOOKS_HOME/hive.env"\nfi\n'
    )
    cli.chmod(0o755)
    home = tmp_path / "state"
    home.mkdir()
    credential = tmp_path / "member"
    credential.write_text("fixture member credential\n")
    code = tmp_path / "code"
    code.write_text("fixture-code\n")
    repo = tmp_path / "source"
    subprocess.run(["git", "init", "--initial-branch=dev", str(repo)], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@example.test",
            "commit",
            "--allow-empty",
            "-m",
            "fixture",
        ],
        check=True,
        capture_output=True,
    )
    env = {
        **os.environ,
        "PATH": f"{tools}:{os.environ['PATH']}",
        "AGENTIHOOKS_HOME": str(home),
        "SWARM_HIVE_ID": "worker-pod",
        "HIVE_REPO_ROOT": str(tmp_path / "repos"),
        "HIVE_REPOSITORIES": json.dumps([{"url": str(repo), "name": "checkout", "ref": "dev"}]),
        "HIVE_JOIN_URL": "https://join.example.test",
        "HIVE_JOIN_CODE_FILE": str(code),
        "HIVE_CREDENTIAL_FILE": str(credential) if joined else "",
        "COMMAND_LOG": str(log),
        "REFUSE_JOIN": "yes" if refused else "no",
    }
    result = subprocess.run(["bash", str(START)], env=env, capture_output=True, text=True)
    return result, log.read_text() if log.exists() else "", home, tmp_path / "repos/checkout"


def test_code_join_clones_repo_and_configures_before_run(tmp_path):
    result, commands, home, repo = startup(tmp_path)
    assert result.returncode == 0, result.stderr
    assert commands.splitlines() == [
        "hive join https://join.example.test fixture-code",
        "hive set worker-pod ui=no ephemeral=yes roles=eng,ci,plan max-agents=1",
        "hive run",
    ]
    assert (repo / ".git").is_dir()
    assert (home / "hive.env").exists()


def test_mounted_member_skips_join_and_keeps_private_credentials(tmp_path):
    result, commands, home, _ = startup(tmp_path, joined=True)
    assert result.returncode == 0, result.stderr
    assert "hive join" not in commands
    assert commands.endswith("hive run\n")
    assert (home / "hive.env").stat().st_mode & 0o777 == 0o600
    assert "fixture member credential" not in result.stdout + result.stderr


def test_refused_join_never_runs_the_worker(tmp_path):
    result, commands, _, _ = startup(tmp_path, refused=True)
    assert result.returncode == 9
    assert "hive run" not in commands
    assert "hive set" not in commands
