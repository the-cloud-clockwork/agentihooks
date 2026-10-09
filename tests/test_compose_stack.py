from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
COMPOSE = yaml.safe_load((ROOT / "compose.yaml").read_text())
SERVICES = COMPOSE["services"]


def test_controller_service_runs_the_controller_loop():
    controller = SERVICES["controller"]
    assert controller["command"][-1].endswith("exec agentihooks controller run")
    assert controller["depends_on"]["redis"]["condition"] == "service_healthy"


def test_controller_issues_its_ledger_service_credential_before_it_runs():
    start = SERVICES["controller"]["command"][-1]
    assert start.index("agentihooks hive controller") < start.index("export AGENTIHOOKS_CONTROLLER_CREDENTIAL")
    assert "swarm:8765" in SERVICES["swarm"]["environment"]["SWARM_ALLOWED_HOSTS"]


def test_redis_starts_from_an_acl_file_with_optional_tls():
    start = (ROOT / "deploy/compose/redis/start.sh").read_text()
    assert "--aclfile" in start
    assert "--tls-port" in start
    assert "user default off" in (ROOT / "deploy/compose/redis/users.acl.template").read_text()


def test_published_ports_bind_to_the_swarm_bind_address():
    for name in ("swarm", "redis", "hive"):
        for port in SERVICES[name]["ports"]:
            assert port.startswith("${SWARM_BIND:-127.0.0.1}:"), (name, port)


def test_swarm_auth_mode_is_removed():
    assert "SWARM_AUTH_MODE" not in (ROOT / "compose.yaml").read_text()
    assert "SWARM_AUTH_MODE" not in (ROOT / "Dockerfile").read_text()


def test_env_example_selects_compose_deployment():
    lines = (ROOT / "deploy/compose/.env.example").read_text().splitlines()
    assert "AGENTIHOOKS_DEPLOYMENT=compose" in lines


def test_swarm_smoke_runs_the_compose_hive_proof():
    jobs = yaml.safe_load((ROOT / ".github/workflows/swarm-smoke.yml").read_text())["jobs"]
    assert any("compose-hive-smoke.sh" in step.get("run", "") for step in jobs["compose-hive"]["steps"])
