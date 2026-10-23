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
    assert SERVICES["swarm"]["environment"]["SWARM_ALLOWED_HOSTS"] == "swarm:8765,${SWARM_ALLOWED_HOSTS:-}"


def test_controller_reads_unhealthy_once_a_swarm_lease_goes_stale():
    probe = SERVICES["controller"]["healthcheck"]["test"][-1]
    assert "lease.current" in probe and "slugs()" in probe


def test_redis_starts_from_an_acl_file_with_optional_tls():
    start = (ROOT / "deploy/compose/redis/start.sh").read_text()
    assert "--aclfile" in start
    assert "--tls-port" in start
    users = (ROOT / "deploy/compose/redis/users.acl.template").read_text().splitlines()
    assert users[0] == "user default off ~* &* +@all"


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
    assert any("compose-hive-smoke.sh" in step.get("run", "") for job in jobs.values() for step in job["steps"])


def test_hive_join_retries_the_redis_mirror_pull_before_starting_redis():
    jobs = yaml.safe_load((ROOT / ".github/workflows/swarm-smoke.yml").read_text())["jobs"]
    assert any("hive-join-smoke.sh" in step.get("run", "") for job in jobs.values() for step in job["steps"])
    script = (ROOT / "scripts/packaging/hive-join-smoke.sh").read_text()
    pull = "bash scripts/packaging/pull-runtime-image.sh mirror.gcr.io/library/redis:7-alpine\n"
    start = 'docker run --pull never -d --name "$run-redis"'
    retry = (ROOT / "scripts/packaging/pull-runtime-image.sh").read_text()
    assert "attempts=3" in retry
    assert 'for attempt in $(seq "$attempts"); do' in retry
    assert 'if acquire "$@"; then' in retry
    assert 'if (( attempt == attempts )); then\n    exit "$status"\n  fi' in retry
    assert "sleep 10" in retry
    assert script.index(pull) < script.index(start)


def test_swarm_init_ships_the_repository_stack_byte_for_byte():
    bundled = ROOT / "scripts/swarm/stack"
    shipped = sorted(str(path.relative_to(bundled)) for path in bundled.rglob("*") if path.is_file())
    assert shipped == [
        "compose.yaml",
        "deploy/compose/adopt.yaml",
        "deploy/compose/redis/healthcheck.sh",
        "deploy/compose/redis/start.sh",
        "deploy/compose/redis/users.acl.template",
    ]
    for name in shipped:
        assert (bundled / name).read_bytes() == (ROOT / name).read_bytes(), name
    check = (ROOT / "deploy/compose/redis/healthcheck.sh").read_text()
    assert 'REDISCLI_AUTH="$SWARM_REDIS_PASSWORD" redis-cli' in check and "--pass" not in check
    for service in ("swarm", "controller", "hive"):
        assert SERVICES[service]["image"] == "ghcr.io/the-cloud-clockwork/agentihooks-swarm:${SWARM_IMAGE_TAG:-dev}"


def test_nats_service_runs_the_pinned_server_with_jetstream_on_a_volume():
    nats = SERVICES["nats"]
    pinned = (ROOT / ".github/actions/nats-server/action.yml").read_text()
    assert "NATS_SERVER_RELEASE: v2.15.1" in pinned
    assert nats["image"] == "mirror.gcr.io/library/nats:2.15.1-alpine"
    assert nats["command"] == ["--jetstream", "--store_dir=/data", "--http_port=8222"]
    assert nats["volumes"] == ["nats-data:/data"]
    assert "nats-data" in COMPOSE["volumes"]
    assert "ports" not in nats
    assert nats["healthcheck"]["test"] == [
        "CMD",
        "wget",
        "-q",
        "-O-",
        "http://127.0.0.1:8222/healthz?js-enabled-only=true",
    ]
    assert nats["restart"] == "unless-stopped"


def test_swarm_services_reach_nats_through_the_bus_setting():
    shared = COMPOSE["x-swarm-env"]
    assert shared["AGENTIHOOKS_BUS"] == "${AGENTIHOOKS_BUS:-nats}"
    assert shared["AGENTIHOOKS_BUS_URL"] == "${AGENTIHOOKS_BUS_URL:-nats://nats:4222}"
    for name in ("swarm", "controller", "hive"):
        service = SERVICES[name]
        assert service["environment"]["AGENTIHOOKS_BUS"] == "${AGENTIHOOKS_BUS:-nats}", name
        assert service["environment"]["AGENTIHOOKS_BUS_URL"] == "${AGENTIHOOKS_BUS_URL:-nats://nats:4222}", name
        assert service["depends_on"]["nats"]["condition"] == "service_healthy", name
