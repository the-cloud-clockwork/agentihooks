import json
import os
import ssl
import subprocess
from pathlib import Path

import pytest

from scripts.swarm_ledger import ledger_link
from tests.swarm_ledger import test_ledger_tls

pytestmark = pytest.mark.unit
cert = test_ledger_tls.cert
ROOT = Path(__file__).resolve().parents[2]
START = ROOT / "deploy/worker/start.sh"
TOKEN = "fixture-github-token-value"


def startup(tmp_path, *, joined=False, refuse="", ssh=False, sshd_dies=False, bundle=False, bundle_url=""):
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
        '  [[ "$REFUSE" != join ]] || exit 9\n'
        '  touch "$AGENTIHOOKS_HOME/hive.env"\nfi\n'
        'if [[ "$*" == "bundle unlink" ]]; then rm -f "$AGENTIHOOKS_HOME/linked-bundle"; fi\n'
        'if [[ "$1" == init ]]; then\n'
        '  printf "hosted ledger CA\\n" > "$AGENTIHOOKS_HOME/ledger-ca.pem"\n'
        '  [[ "$*" != "init --bundle "* ]] || printf %s "$3" > "$AGENTIHOOKS_HOME/linked-bundle"\n'
        '  [[ "$REFUSE" != init ]] || exit 7\n'
        '  [[ "$REFUSE" != bundle-init || "$*" == init ]] || exit 7\nfi\n'
        'if [[ "$*" == "deps ensure" ]]; then [[ "$REFUSE" != deps ]] || exit 4; fi\n'
        'if [[ "$*" == "hive run" && "${GH_TOKEN:-}" == "$GITHUB_PERSONAL_ACCESS_TOKEN" ]]; then\n'
        '  touch "$AGENTIHOOKS_HOME/gh-token-matches"\nfi\n'
    )
    cli.chmod(0o755)
    home = tmp_path / "state"
    home.mkdir()
    (tmp_path / "home").mkdir()
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
        "HOME": str(tmp_path / "home"),
        "GITHUB_PERSONAL_ACCESS_TOKEN": TOKEN,
        "GITHUB_TOKEN": TOKEN,
        "HIVE_BUNDLE_URL": str(repo) if bundle else bundle_url,
        "HIVE_BUNDLE_REF": "dev",
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
        "REFUSE": refuse,
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
        "bundle unlink",
        "init",
        "deps ensure",
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
    assert lines[1:5] == [
        f"sshd -t -f {state}/sshd_config",
        f"sshd -D -e -f {state}/sshd_config",
        "bundle unlink",
        "init",
    ]
    assert (state / "host_key").stat().st_mode & 0o777 == 0o600
    assert f"AuthorizedKeysFile {tmp_path}/keys/authorized_keys\n" in config
    assert "Port 2222\n" in config
    assert "AllowTcpForwarding no\n" in config
    assert "PasswordAuthentication no\n" in config


def test_an_sshd_that_dies_at_start_never_runs_the_worker(tmp_path):
    result, commands, _, _ = startup(tmp_path, ssh=True, sshd_dies=True)
    assert result.returncode != 0
    assert "init" not in commands.splitlines()


def test_failed_profile_init_never_runs_the_worker(tmp_path):
    result, commands, _, _ = startup(tmp_path, refuse="init")
    assert result.returncode == 7
    assert commands.splitlines()[-1] == "init"


def test_failed_init_with_the_bundle_runs_again_on_the_package_roles(tmp_path):
    result, commands, home, repo = startup(tmp_path, refuse="bundle-init", bundle=True)
    assert result.returncode == 0, result.stderr
    assert (
        "worker-hive: agentihooks init with the bundle failed; init runs on the package roles alone\n" in result.stderr
    )
    assert commands.splitlines()[1:5] == [
        f"init --bundle {repo.parent / 'agentihooks-bundle'}",
        "bundle unlink",
        "init",
        "deps ensure",
    ]
    assert not (home / "linked-bundle").exists()
    assert commands.endswith("hive run\n")


def test_failed_init_with_the_bundle_and_on_the_package_roles_never_runs_the_worker(tmp_path):
    result, commands, _, _ = startup(tmp_path, refuse="init", bundle=True)
    assert result.returncode == 7
    assert commands.splitlines()[-1] == "init"
    assert "hive set" not in commands


def test_mounted_member_skips_join_and_keeps_private_credentials(tmp_path):
    result, commands, home, _ = startup(tmp_path, joined=True)
    assert result.returncode == 0, result.stderr
    assert "hive join" not in commands
    assert "hive set worker-member ui=no ephemeral=yes roles=eng,ci,plan,dispatch max-agents=1\n" in commands
    assert commands.endswith("hive run\n")
    assert (home / "hive.env").stat().st_mode & 0o777 == 0o600
    assert "fixture member credential" not in result.stdout + result.stderr


def test_refused_join_never_runs_the_worker(tmp_path):
    result, commands, _, _ = startup(tmp_path, refuse="join")
    assert result.returncode == 9
    assert "hive run" not in commands
    assert "hive set" not in commands


def test_bundle_is_cloned_beside_the_repos_and_linked_by_init(tmp_path):
    result, commands, _, repo = startup(tmp_path, bundle=True)
    assert result.returncode == 0, result.stderr
    bundle = repo.parent / "agentihooks-bundle"
    assert (bundle / ".git").is_dir()
    branch = subprocess.run(["git", "-C", str(bundle), "branch", "--show-current"], capture_output=True, text=True)
    assert branch.stdout == "dev\n"
    assert commands.splitlines()[1:3] == [f"init --bundle {bundle}", "deps ensure"]
    assert (repo.parent.parent / "state/linked-bundle").read_text() == str(bundle)
    assert commands.endswith("hive run\n")


def test_github_token_reaches_git_by_reference_and_is_never_written(tmp_path):
    result, _, home, repo = startup(tmp_path, bundle=True)
    assert result.returncode == 0, result.stderr
    config = (tmp_path / "home/.gitconfig").read_text()
    assert "scripts.github_app_cli credential" in config
    assert (home / "gh-token-matches").exists()
    assert "GH_TOKEN" not in (home / "hive-container.env").read_text()
    written = [path for root in (tmp_path / "home", home, repo.parent) for path in root.rglob("*") if path.is_file()]
    assert not [path for path in written if TOKEN.encode() in path.read_bytes()]
    assert TOKEN not in result.stdout + result.stderr
    fill = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        env={**os.environ, "HOME": str(tmp_path / "home"), "HIVE_GITHUB_AUTH_MODE": "token", "GITHUB_TOKEN": TOKEN},
        capture_output=True,
        text=True,
    )
    assert fill.returncode == 0, fill.stderr
    assert f"username=x-access-token\npassword={TOKEN}\n" in fill.stdout


def test_failed_deps_ensure_still_runs_the_hive(tmp_path):
    result, commands, _, _ = startup(tmp_path, bundle=True, refuse="deps")
    assert result.returncode == 0, result.stderr
    assert "worker-hive: agentihooks deps ensure failed" in result.stderr
    assert commands.endswith("hive run\n")


def test_a_failed_bundle_clone_is_logged_and_the_hive_runs_on_the_package_roles(tmp_path):
    result, commands, _, repo = startup(tmp_path, bundle_url=str(tmp_path / "missing-bundle"))
    assert result.returncode == 0, result.stderr
    assert "worker-hive: bundle clone failed; init runs on the package roles alone\n" in result.stderr
    assert not (repo.parent / "agentihooks-bundle").exists()
    assert commands.splitlines()[1:4] == ["bundle unlink", "init", "deps ensure"]
    assert commands.endswith("hive run\n")


def test_an_emptied_origin_unlinks_the_bundle_a_persistent_home_kept(tmp_path):
    kept = tmp_path / "repos/agentihooks-bundle"
    (kept / ".git").mkdir(parents=True)
    result, commands, _, _ = startup(tmp_path)
    assert result.returncode == 0, result.stderr
    assert f"init --bundle {kept}" not in commands
    assert commands.splitlines()[1:4] == ["bundle unlink", "init", "deps ensure"]


def test_the_mounted_ledger_ca_replaces_the_one_init_wrote_with_the_system_roots_kept(tmp_path, monkeypatch, cert):
    pem = Path(cert[0]).read_text()
    monkeypatch.setenv("LEDGER_CA_PEM", pem)
    result, _, home, _ = startup(tmp_path)
    assert result.returncode == 0, result.stderr
    placed = (home / "ledger-ca.pem").read_text()
    assert "hosted ledger CA" not in placed
    assert placed.endswith(pem.strip() + "\n")
    assert placed.startswith(Path(ssl.get_default_verify_paths().cafile).read_text())
    assert ledger_link.transport({"AGENTIHOOKS_HOME": str(home)})["context"].cert_store_stats()["x509_ca"] > 1


def test_without_a_mounted_ledger_ca_the_one_init_wrote_stays(tmp_path):
    result, _, home, _ = startup(tmp_path)
    assert result.returncode == 0, result.stderr
    assert (home / "ledger-ca.pem").read_text() == "hosted ledger CA\n"
