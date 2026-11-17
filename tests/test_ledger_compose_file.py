import json
import subprocess
from pathlib import Path

import yaml

STACK = yaml.safe_load((Path(__file__).resolve().parents[1] / "deploy/compose/ledger.yaml").read_text())
SERVICE = STACK["services"]["ledger"]


def test_the_rendered_ledger_container_limits_malloc_arenas(tmp_path):
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--file",
            str(Path(__file__).parents[1] / "deploy" / "compose" / "ledger.yaml"),
            "config",
            "--format",
            "json",
        ],
        env={
            "HOME": str(tmp_path),
            "LEDGER_CODE": str(tmp_path),
            "LEDGER_DIR": str(tmp_path),
            "MALLOC_ARENA_MAX": "7",
        },
        check=True,
        capture_output=True,
        text=True,
    )
    service = json.loads(result.stdout)["services"]["ledger"]
    assert service["environment"]["MALLOC_ARENA_MAX"] == "2"


def test_the_service_caps_memory_cpu_and_threads_and_restarts_itself():
    assert SERVICE["mem_limit"] == "1536m"
    assert SERVICE["memswap_limit"] == "1536m"
    assert SERVICE["cpus"] == 2
    assert SERVICE["pids_limit"] == 200
    assert SERVICE["restart"] == "unless-stopped"


def test_the_healthcheck_runs_the_write_probe_on_the_checkout_code():
    check = SERVICE["healthcheck"]
    # Named in parts: the mutation gate selects tests naming a changed module, and its workspace has no deploy folder.
    assert check["test"] == ["CMD", "python", "-m", ".".join(["scripts", "swarm_ledger", "ledger_health"])]
    assert (check["interval"], check["timeout"], check["retries"], check["start_period"]) == ("20s", "10s", 3, "60s")
    assert SERVICE["working_dir"].startswith("${LEDGER_CODE:?")


def test_the_server_runs_the_checkout_code_on_the_host_network_and_pids():
    assert STACK["name"] == "agentihooks-ledger"
    assert SERVICE["image"] == "ghcr.io/the-cloud-clockwork/agentihooks-swarm:dev"
    assert SERVICE["command"] == ["python", "${LEDGER_CODE}/scripts/swarm_ledger/server_supervisor.py"]
    assert (SERVICE["network_mode"], SERVICE["pid"]) == ("host", "host")
    assert SERVICE["user"] == "${LEDGER_UID:-1000}:${LEDGER_GID:-1000}"
    env = SERVICE["environment"]
    assert env["LEDGER_DIR"].startswith("${LEDGER_DIR:?")
    assert (env["HOME"], env["PYTHONPATH"], env["LEDGER_HOST"], env["LEDGER_PORT"], env["SWARM_RELOAD"]) == (
        "${HOME}",
        "${LEDGER_CODE}",
        "127.0.0.1",
        "${LEDGER_READ_PORT:-18765}",
        "1",
    )


def test_the_ledger_folder_is_mounted_from_the_host_at_its_own_path():
    assert SERVICE["volumes"] == [
        "${LEDGER_DIR}:${LEDGER_DIR}",
        "${LEDGER_CODE}:${LEDGER_CODE}:ro",
        "${HOME}/.agentihooks:${HOME}/.agentihooks",
    ]


def test_the_api_service_runs_the_write_process_on_its_own_loopback_port() -> None:
    api = STACK["services"]["api"]
    assert api["command"] == ["python", "${LEDGER_CODE}/scripts/swarm_ledger/api_service.py", "--serve"]
    assert (api["network_mode"], api["pid"]) == ("host", "host")
    env = api["environment"]
    assert env["LEDGER_API_HOST"] == "127.0.0.1"
    assert env["LEDGER_API_PORT"] == "${LEDGER_API_PORT:-8766}"
    assert env["AGENTIHOOKS_SWARM_DATABASE_URL"] == "${AGENTIHOOKS_SWARM_DATABASE_URL:-}"


def test_the_front_listens_on_the_published_port_the_ledger_used_to_own() -> None:
    front = STACK["services"]["front"]
    assert front["network_mode"] == "host"
    env = front["environment"]
    assert env["LEDGER_PORT"] == "${LEDGER_PORT:-8765}"
    assert env["LEDGER_READ_PORT"] == "${LEDGER_READ_PORT:-18765}"
    assert env["LEDGER_API_PORT"] == "${LEDGER_API_PORT:-8766}"
    assert front["depends_on"]["ledger"]["condition"] == "service_healthy"
    assert front["depends_on"]["api"]["condition"] == "service_healthy"
    template = (Path(__file__).resolve().parents[1] / "deploy/compose/ledger-local-front.conf.template").read_text()
    assert "${LEDGER_PORT}" in template
    assert "${LEDGER_READ_PORT}" in template
    assert "${LEDGER_API_PORT}" in template
    assert "GET|HEAD" in template
