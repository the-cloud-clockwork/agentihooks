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


JOINED = "AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL=fixture-ledger\nAGENTIHOOKS_HIVE_REDIS_URL=redis://joined\n"
REFRESHED = "AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL=fixture-ledger\nAGENTIHOOKS_HIVE_REDIS_URL=redis://refreshed\n"
OTHER = "AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL=fixture-old-ledger\nAGENTIHOOKS_HIVE_REDIS_URL=redis://old\n"
MOVED = "AGENTIHOOKS_HIVE_URL=https://tailnet.example:8770\n" + REFRESHED
SAVED = {"refreshed": REFRESHED, "rejoined": OTHER, "moved": MOVED}


def startup(tmp_path, *, credential="mounted", refuse="", ssh=False, sshd_dies=False, bundle=False, bundle_url=""):
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
        'if [[ "$*" == "bundle unlink" ]]; then rm -f "$AGENTIHOOKS_HOME/linked-bundle"; fi\n'
        'if [[ "$1" == init ]]; then\n'
        '  printf "hosted ledger CA\\n" > "$AGENTIHOOKS_HOME/ledger-ca.pem"\n'
        '  [[ "$*" != "init --bundle "* ]] || printf %s "$3" > "$AGENTIHOOKS_HOME/linked-bundle"\n'
        '  [[ "$REFUSE" != init ]] || exit 7\n'
        '  [[ "$REFUSE" != bundle-init || "$*" == init ]] || exit 7\nfi\n'
        'if [[ "$1 $2" == "hive join" ]]; then [[ "$REFUSE" != join ]] || exit 9; fi\n'
        'if [[ "$*" == "deps ensure" ]]; then [[ "$REFUSE" != deps ]] || exit 4; fi\n'
        'if [[ "$*" == "hive run" ]]; then printf %s "${LEDGER_CA_PEM:+set}" > "$AGENTIHOOKS_HOME/hive-run-ca-pem"; fi\n'
        'if [[ "$*" == "hive run" && "${GH_TOKEN:-}" == "$GITHUB_PERSONAL_ACCESS_TOKEN" ]]; then\n'
        '  touch "$AGENTIHOOKS_HOME/gh-token-matches"\nfi\n'
    )
    cli.chmod(0o755)
    home = tmp_path / "state"
    home.mkdir()
    (tmp_path / "home").mkdir(exist_ok=True)
    member = tmp_path / "member"
    member.write_text(JOINED if credential in SAVED else "fixture member credential\n")
    if credential == "delivered":
        (home / "hive.env").write_text("fixture delivered hive env\n")
    if credential in SAVED:
        (home / "hive.env").write_text(SAVED[credential])
        (home / "hive.env").chmod(0o600)
        credential = "mounted"
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
        "HIVE_CREDENTIAL_FILE": str(member) if credential == "mounted" else "",
        "COMMAND_LOG": str(log),
        "REFUSE": refuse,
        "AH_CC_TOKEN_fixture": "fixture $token (value)",
        "AH_CC_TOKEN_quoted": "it's quoted",
        "AH_CC_TOKEN_escaped": "back\\\\slash",
        "SWARM_SECRET": "fixture tick secret",
        "LANGFUSE_PUBLIC_KEY": "fixture-langfuse-public",
        "LANGFUSE_SECRET_KEY": "fixture-langfuse-private",
        "LANGFUSE_HOST": "https://langfuse.example.test",
        "SSHD_DIES": "yes" if sshd_dies else "no",
    }
    if credential != "mounted":
        env["HIVE_DELIVERY_WAIT_S"] = "0"
    if ssh:
        env |= {
            "HIVE_SSHD": str(sshd),
            "HIVE_SSH_AUTHORIZED_KEYS_FILE": str(keys / "authorized_keys"),
            "HIVE_SSH_HOST_KEY_FILE": str(keys / "host_key"),
        }
    result = subprocess.run(["bash", str(START)], env=env, capture_output=True, text=True)
    return result, log.read_text() if log.exists() else "", home, tmp_path / "repos/checkout"


RUN = [
    "bundle unlink",
    "init",
    "deps ensure",
    "hive set --keep worker-member ui=no ephemeral=yes draining=no excluded=master max-agents=20",
    "hive run",
]


def test_mounted_member_clones_repo_and_configures_before_run(tmp_path):
    result, commands, home, repo = startup(tmp_path)
    assert result.returncode == 0, result.stderr
    assert commands.splitlines() == RUN
    assert (repo / ".git").is_dir()
    assert (home / "hive.env").stat().st_mode & 0o777 == 0o600
    assert "fixture member credential" not in result.stdout + result.stderr
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


def test_the_mounted_invite_is_joined_before_worker_configuration(tmp_path):
    result, commands, home, _ = startup(tmp_path)
    assert result.returncode == 0, result.stderr
    joined = f"hive join {home / 'hive.env'}"
    assert joined in commands.splitlines()
    assert commands.index(joined) < commands.index("hive set") < commands.index("hive run")


def test_a_refused_invite_never_runs_the_worker(tmp_path):
    result, commands, _, _ = startup(tmp_path, refuse="join")
    assert result.returncode != 0
    assert "hive set" not in commands
    assert "hive run" not in commands


def test_the_container_env_carries_the_langfuse_settings_to_sshd_sessions(tmp_path):
    result, _, home, _ = startup(tmp_path)
    assert result.returncode == 0, result.stderr
    sourced = subprocess.run(
        [
            "env",
            "-i",
            "bash",
            "-c",
            'set -a; . "$1"; printf "%s|%s|%s" "$LANGFUSE_PUBLIC_KEY" "$LANGFUSE_SECRET_KEY" "$LANGFUSE_HOST"',
            "_",
            str(home / "hive-container.env"),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert sourced.stdout == "fixture-langfuse-public|fixture-langfuse-private|https://langfuse.example.test"


def test_the_container_env_carries_brain_clients_to_sshd_sessions(tmp_path, monkeypatch):
    from dotenv import dotenv_values

    brain = {
        "BRAIN_URL": "http://brain.example.test:8080",
        "BRAIN_ENABLED": "true",
        "KB_ROUTER_TOKEN": "fixture-brain-client",
        "AMYGDALA_ENABLED": "true",
        "SWARM_BRAIN_MCP_KEY": "fixture-brain-mcp",
    }
    for name, value in brain.items():
        monkeypatch.setenv(name, value)
    result, _, home, _ = startup(tmp_path)
    assert result.returncode == 0, result.stderr
    saved = dotenv_values(home / "hive-container.env")
    assert {name: saved.get(name) for name in brain} == brain


def test_a_delivered_hive_env_runs_without_waiting(tmp_path):
    result, commands, home, _ = startup(tmp_path, credential="delivered")
    assert result.returncode == 0, result.stderr
    assert commands.splitlines() == RUN
    assert (home / "hive.env").read_text() == "fixture delivered hive env\n"


def test_a_restart_keeps_the_refreshed_credential_of_the_same_member(tmp_path):
    result, _, home, _ = startup(tmp_path, credential="refreshed")

    assert result.returncode == 0, result.stderr
    assert (home / "hive.env").read_text() == REFRESHED


def test_a_new_join_replaces_the_saved_credential_of_another_member(tmp_path):
    result, _, home, _ = startup(tmp_path, credential="rejoined")

    assert result.returncode == 0, result.stderr
    assert (home / "hive.env").read_text() == JOINED
    assert (home / "hive.env").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    ("credential", "hive_url", "saved"),
    [
        ("moved", "https://anton.example:8770", MOVED.replace("tailnet.example", "anton.example")),
        ("moved", "", MOVED),
        (
            "refreshed",
            "https://anton.example:8770/?a=1&b=2",
            "AGENTIHOOKS_HIVE_URL=https://anton.example:8770/?a=1&b=2\n" + REFRESHED,
        ),
    ],
)
def test_the_charted_hive_url_replaces_the_endpoint_in_the_saved_credential(
    tmp_path, monkeypatch, credential, hive_url, saved
):
    monkeypatch.delenv("AGENTIHOOKS_HIVE_URL", raising=False)
    if hive_url:
        monkeypatch.setenv("AGENTIHOOKS_HIVE_URL", hive_url)
    result, _, home, _ = startup(tmp_path, credential=credential)

    assert result.returncode == 0, result.stderr
    assert (home / "hive.env").read_text() == saved
    assert (home / "hive.env").stat().st_mode & 0o777 == 0o600


def test_no_delivered_credential_by_the_deadline_never_runs_the_worker(tmp_path):
    result, commands, _, _ = startup(tmp_path, credential="none")
    assert result.returncode == 1
    assert commands == ""
    assert result.stderr.splitlines()[-1] == (
        "worker-hive: no member credential was delivered within 0s; run agentihooks hive add from the UI hive"
    )


def test_ssh_serves_the_mounted_keys_before_waiting_for_a_delivered_credential(tmp_path):
    result, commands, home, _ = startup(tmp_path, credential="none", ssh=True)
    assert result.returncode == 1
    state = home / "ssh"
    assert commands.splitlines() == [f"sshd -t -f {state}/sshd_config", f"sshd -D -e -f {state}/sshd_config"]


def test_ssh_checks_its_config_then_serves_the_mounted_keys_before_init(tmp_path):
    result, commands, home, _ = startup(tmp_path, ssh=True)
    assert result.returncode == 0, result.stderr
    state = home / "ssh"
    config = (state / "sshd_config").read_text()
    assert commands.splitlines()[0:4] == [
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
    assert commands.splitlines()[0:4] == [
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


def test_bundle_is_cloned_beside_the_repos_and_linked_by_init(tmp_path):
    result, commands, _, repo = startup(tmp_path, bundle=True)
    assert result.returncode == 0, result.stderr
    bundle = repo.parent / "agentihooks-bundle"
    assert (bundle / ".git").is_dir()
    branch = subprocess.run(["git", "-C", str(bundle), "branch", "--show-current"], capture_output=True, text=True)
    assert branch.stdout == "dev\n"
    assert commands.splitlines()[0:2] == [f"init --bundle {bundle}", "deps ensure"]
    assert (repo.parent.parent / "state/linked-bundle").read_text() == str(bundle)
    assert commands.endswith("hive run\n")


def test_linked_profiles_are_cloned_and_registered_before_init(tmp_path, monkeypatch):
    origin = tmp_path / "linked-origin"
    origin.mkdir()
    subprocess.run(["git", "init", "--initial-branch=dev", str(origin)], check=True, capture_output=True)
    profile = origin / "profiles" / "memory"
    profile.mkdir(parents=True)
    (profile / "profile.yml").write_text("name: memory\n")
    subprocess.run(["git", "-C", str(origin), "add", "profiles/memory/profile.yml"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(origin),
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@example.test",
            "commit",
            "-m",
            "fixture",
        ],
        check=True,
        capture_output=True,
    )
    monkeypatch.setenv(
        "HIVE_LINKED_PROFILES",
        json.dumps([{"name": "memory", "url": str(origin), "ref": "dev", "path": "profiles/memory"}]),
    )
    worker = tmp_path / "worker"
    worker.mkdir()
    result, commands, home, _ = startup(worker)
    assert result.returncode == 0, result.stderr
    cloned = next((worker / "repos/.linked-profiles/memory").glob("*/profiles/memory"))
    assert (cloned / "profile.yml").read_text() == "name: memory\n"
    registration = f"link-profile link {cloned} --name memory --no-append --no-init"
    assert commands.splitlines().index(registration) < commands.splitlines().index("init")
    assert "HIVE_LINKED_PROFILES=" in (home / "hive-container.env").read_text()


def test_a_failed_linked_profile_clone_refuses_hive_start(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "HIVE_LINKED_PROFILES",
        json.dumps([{"name": "memory", "url": str(tmp_path / "absent"), "ref": "dev", "path": "."}]),
    )
    result, commands, _, _ = startup(tmp_path)
    assert result.returncode != 0
    assert "init" not in commands.splitlines()
    assert "hive run" not in commands.splitlines()


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
    assert commands.splitlines()[0:3] == ["bundle unlink", "init", "deps ensure"]
    assert commands.endswith("hive run\n")


def test_an_emptied_origin_unlinks_the_bundle_a_persistent_home_kept(tmp_path):
    kept = tmp_path / "repos/agentihooks-bundle"
    (kept / ".git").mkdir(parents=True)
    result, commands, _, _ = startup(tmp_path)
    assert result.returncode == 0, result.stderr
    assert f"init --bundle {kept}" not in commands
    assert commands.splitlines()[0:3] == ["bundle unlink", "init", "deps ensure"]


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


def test_without_a_mounted_ledger_ca_the_one_init_wrote_stays(tmp_path, monkeypatch):
    monkeypatch.delenv("LEDGER_CA_PEM", raising=False)
    result, _, home, _ = startup(tmp_path)
    assert result.returncode == 0, result.stderr
    assert (home / "ledger-ca.pem").read_text() == "hosted ledger CA\n"


@pytest.mark.parametrize("mounted_first", [False, True])
def test_ledger_ca_cases_ignore_inherited_mount_in_either_order(tmp_path, monkeypatch, cert, mounted_first):
    monkeypatch.setenv("LEDGER_CA_PEM", Path(cert[0]).read_text())
    cases = [
        test_without_a_mounted_ledger_ca_the_one_init_wrote_stays,
        test_the_mounted_ledger_ca_replaces_the_one_init_wrote_with_the_system_roots_kept,
    ]
    if mounted_first:
        cases.reverse()
    for index, case in enumerate(cases):
        root = tmp_path / str(index)
        root.mkdir()
        with monkeypatch.context() as isolated:
            if case is test_without_a_mounted_ledger_ca_the_one_init_wrote_stays:
                case(root, isolated)
            else:
                case(root, isolated, cert)


def test_the_workspace_venv_of_the_repo_instructions_resolves_to_the_image_python(tmp_path):
    result, _, _, _ = startup(tmp_path)
    assert result.returncode == 0, result.stderr
    prefix = subprocess.run(
        ["python3", "-c", "import sys; print(sys.prefix)"], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert (tmp_path / "home/dev/tcc-ecosystem/.venv").resolve() == Path(prefix).resolve()


def git_identity(tmp_path):
    env = {**os.environ, "HOME": str(tmp_path / "home"), "GIT_CONFIG_NOSYSTEM": "1"}
    read = [["git", "config", "--global", key] for key in ("user.name", "user.email")]
    return [subprocess.run(argv, env=env, capture_output=True, text=True).stdout.strip() for argv in read]


def test_a_hive_without_a_git_identity_commits_as_the_hive(tmp_path):
    result, _, _, _ = startup(tmp_path)
    assert result.returncode == 0, result.stderr
    assert git_identity(tmp_path) == ["agentihooks hive", "agentihooks-hive@users.noreply.github.com"]


def test_a_configured_git_identity_is_kept(tmp_path):
    (tmp_path / "home").mkdir()
    (tmp_path / "home/.gitconfig").write_text("[user]\n\tname = Operator\n\temail = operator@example.test\n")
    result, _, _, _ = startup(tmp_path)
    assert result.returncode == 0, result.stderr
    assert git_identity(tmp_path) == ["Operator", "operator@example.test"]


def test_hive_run_and_the_agents_it_launches_never_inherit_the_inline_ledger_ca(tmp_path, monkeypatch, cert):
    monkeypatch.setenv("LEDGER_CA_PEM", Path(cert[0]).read_text())
    result, _, home, _ = startup(tmp_path)
    assert result.returncode == 0, result.stderr
    assert (home / "hive-run-ca-pem").read_text() == ""
