import json
import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
START = ROOT / "deploy/worker/start.sh"


def startup(tmp_path, *, joined=False, refused=False, init_fails=False, ssh=False, sshd_dies=False):
    tools = tmp_path / "bin"
    tools.mkdir()
    log = tmp_path / "commands"
    sshd = tools / "sshd"
    sshd.write_text(
        '#!/usr/bin/env bash\nprintf "sshd %s\\n" "$*" >> "$COMMAND_LOG"\n[[ "$1" != -D ]] || [[ "$SSHD_DIES" == yes ]] || exec sleep 5 >/dev/null 2>&1\n'
    )
    sshd.chmod(0o755)
    keys = tmp_path / "keys"
    keys.mkdir()
    (keys / "authorized_keys").write_text("ssh-ed25519 fixture operator\n")
    (keys / "host_key").write_text("fixture host key\n")
    cli = tools / "agentihooks"
    cli.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        'printf "%s\\n" "$*" >> "$COMMAND_LOG"\n'
        'if [[ "$*" == "hive join "* ]]; then\n'
        '  [[ "$REFUSE_JOIN" == no ]] || exit 9\n'
        '  touch "$AGENTIHOOKS_HOME/hive.env"\nfi\n'
        'if [[ "$1" == init ]]; then [[ "$REFUSE_INIT" == no ]] || exit 7; fi\n'
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
        "AGENTIHOOKS_HIVE_ID": "worker-member",
        "AGENTIHOOKS_DEPLOYMENT": "remote",
        "AGENTIHOOKS_HIVE_REDIS_URL": "redis://localhost:6379/15",
        "HIVE_REPO_ROOT": str(tmp_path / "repos"),
        "HIVE_REPOSITORIES": json.dumps([{"url": str(repo), "name": "checkout", "ref": "dev"}]),
        "HIVE_JOIN_URL": "https://join.example.test",
        "HIVE_JOIN_CODE_FILE": str(code),
        "HIVE_CREDENTIAL_FILE": str(credential) if joined else "",
        "COMMAND_LOG": str(log),
        "REFUSE_JOIN": "yes" if refused else "no",
        "REFUSE_INIT": "yes" if init_fails else "no",
        "AH_CC_TOKEN_fixture": "fixture $token (value)",
        "AH_CC_TOKEN_quoted": "it's quoted",
        "AH_CC_TOKEN_escaped": "back\\\\slash",
        "SWARM_SECRET": "fixture tick secret",
        "SSHD_DIES": "yes" if sshd_dies else "no",
    }
    if ssh:
        env |= {
            "HIVE_SSHD": str(sshd),
            "HIVE_SSH_AUTHORIZED_KEYS_FILE": str(keys / "authorized_keys"),
            "HIVE_SSH_HOST_KEY_FILE": str(keys / "host_key"),
        }
    result = subprocess.run(["bash", str(START)], env=env, capture_output=True, text=True)
    return result, log.read_text() if log.exists() else "", home, tmp_path / "repos/checkout"


def test_code_join_clones_repo_and_configures_before_run(tmp_path):
    result, commands, home, repo = startup(tmp_path)
    assert result.returncode == 0, result.stderr
    assert commands.splitlines() == [
        "hive join https://join.example.test fixture-code",
        "init --profile engineer",
        "hive set worker-member ui=no ephemeral=yes roles=eng,ci,plan,dispatch max-agents=1",
        "hive run",
    ]
    assert (repo / ".git").is_dir()
    assert (home / "hive.env").exists()
    container_env = home / "hive-container.env"
    assert container_env.stat().st_mode & 0o777 == 0o600
    assert "AGENTIHOOKS_HIVE_ID=worker-member\n" in container_env.read_text()
    assert "COMMAND_LOG=" not in container_env.read_text()
    sourced = subprocess.run(
        ["bash", "-c", 'set -a; . "$1"; printf %s "$AH_CC_TOKEN_fixture"', "_", str(container_env)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert sourced.stdout == "fixture $token (value)"
    assert "AH_CC_TOKEN_quoted" not in container_env.read_text()
    assert "AH_CC_TOKEN_quoted holds a quote or backslash" in result.stderr
    assert "AH_CC_TOKEN_escaped=" not in container_env.read_text()
    assert "SWARM_SECRET=" not in container_env.read_text()


def test_ssh_checks_its_config_then_serves_the_mounted_keys_before_init(tmp_path):
    result, commands, home, _ = startup(tmp_path, ssh=True)
    assert result.returncode == 0, result.stderr
    state = home / "ssh"
    config = (state / "sshd_config").read_text()
    lines = commands.splitlines()
    assert lines[1:4] == [
        f"sshd -t -f {state}/sshd_config",
        f"sshd -D -e -f {state}/sshd_config",
        "init --profile engineer",
    ]
    assert (state / "host_key").stat().st_mode & 0o777 == 0o600
    assert f"AuthorizedKeysFile {tmp_path}/keys/authorized_keys\n" in config
    assert "Port 2222\n" in config
    assert "AllowTcpForwarding no\n" in config
    assert "PasswordAuthentication no\n" in config


def test_an_sshd_that_dies_at_start_never_runs_the_worker(tmp_path):
    result, commands, _, _ = startup(tmp_path, ssh=True, sshd_dies=True)
    assert result.returncode != 0
    assert "init --profile engineer" not in commands


def test_failed_profile_init_never_runs_the_worker(tmp_path):
    result, commands, _, _ = startup(tmp_path, init_fails=True)
    assert result.returncode == 7
    assert commands.splitlines()[-1] == "init --profile engineer"


def test_mounted_member_skips_join_and_keeps_private_credentials(tmp_path):
    result, commands, home, _ = startup(tmp_path, joined=True)
    assert result.returncode == 0, result.stderr
    assert "hive join" not in commands
    assert "hive set worker-member ui=no ephemeral=yes roles=eng,ci,plan,dispatch max-agents=1\n" in commands
    assert commands.endswith("hive run\n")
    assert (home / "hive.env").stat().st_mode & 0o777 == 0o600
    assert "fixture member credential" not in result.stdout + result.stderr


def test_refused_join_never_runs_the_worker(tmp_path):
    result, commands, _, _ = startup(tmp_path, refused=True)
    assert result.returncode == 9
    assert "hive run" not in commands
    assert "hive set" not in commands
