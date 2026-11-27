import re
from pathlib import Path

import pytest
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


def test_the_controller_and_hive_keep_scheduler_state_in_the_nats_bucket():
    for name in ("controller", "hive"):
        environment = SERVICES[name]["environment"]
        assert environment["AGENTIHOOKS_SCHEDULER_STATE_BACKEND"] == "${SWARM_SCHEDULER_STATE_BACKEND:-bus}"
        assert environment["AGENTIHOOKS_BUS_URL"] == "${SWARM_BUS_URL:-nats://nats:4222}"
        assert SERVICES[name]["depends_on"]["nats"]["condition"] == "service_healthy"
    assert "--jetstream" in SERVICES["nats"]["command"]


def test_redis_starts_from_an_acl_file_with_optional_tls():
    start = (ROOT / "deploy/compose/redis/start.sh").read_text()
    assert "--aclfile" in start
    assert "--tls-port" in start
    users = (ROOT / "deploy/compose/redis/users.acl.template").read_text().splitlines()
    assert users[0] == "user default off ~* &* +@all"


def test_published_ports_bind_to_the_swarm_bind_address():
    for name in ("front", "redis", "hive"):
        for port in SERVICES[name]["ports"]:
            assert port.startswith("${SWARM_BIND:-127.0.0.1}:"), (name, port)


def test_swarm_auth_mode_is_removed():
    assert "SWARM_AUTH_MODE" not in (ROOT / "compose.yaml").read_text()
    assert "SWARM_AUTH_MODE" not in (ROOT / "Dockerfile").read_text()


def test_env_example_selects_compose_deployment():
    lines = (ROOT / "deploy/compose/.env.example").read_text().splitlines()
    assert "SWARM_DEPLOYMENT=compose" in lines


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
    assert "for ((attempt = 1; attempt <= attempts; attempt++)); do" in retry
    assert 'if acquire "$@"; then' in retry
    assert 'if (( attempt == attempts )); then\n    exit "$status"\n  fi' in retry
    assert 'delay="${PULL_RETRY_SECONDS:-10}"' in retry
    assert 'sleep "$delay"' in retry
    assert script.index(pull) < script.index(start)


def test_swarm_init_ships_the_repository_stack_byte_for_byte():
    bundled = ROOT / "scripts/swarm/stack"
    shipped = sorted(str(path.relative_to(bundled)) for path in bundled.rglob("*") if path.is_file())
    assert shipped == [
        "compose.yaml",
        "deploy/compose/adopt.yaml",
        "deploy/compose/ledger-front.conf",
        "deploy/compose/ledger-tailnet.conf.template",
        "deploy/compose/redis/healthcheck.sh",
        "deploy/compose/redis/start.sh",
        "deploy/compose/redis/users.acl.template",
        "deploy/compose/start-tailnet.sh",
        "deploy/compose/tailnet.yaml",
    ]
    for name in shipped:
        assert (bundled / name).read_bytes() == (ROOT / name).read_bytes(), name
    check = (ROOT / "deploy/compose/redis/healthcheck.sh").read_text()
    assert 'REDISCLI_AUTH="$SWARM_REDIS_PASSWORD" redis-cli' in check and "--pass" not in check
    for service in ("swarm", "api", "controller", "hive"):
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
    smoke = (ROOT / "scripts/packaging/compose-hive-smoke.sh").read_text()
    assert "bash scripts/packaging/pull-runtime-image.sh mirror.gcr.io/library/nats:2.15.1-alpine\n" in smoke


def test_swarm_services_reach_nats_through_the_bus_setting():
    shared = COMPOSE["x-swarm-env"]
    assert shared["AGENTIHOOKS_BUS"] == "${SWARM_BUS:-nats}"
    assert shared["AGENTIHOOKS_BUS_URL"] == "${SWARM_BUS_URL:-nats://nats:4222}"
    for name in ("swarm", "controller", "hive"):
        service = SERVICES[name]
        assert service["environment"]["AGENTIHOOKS_BUS"] == "${SWARM_BUS:-nats}", name
        assert service["environment"]["AGENTIHOOKS_BUS_URL"] == "${SWARM_BUS_URL:-nats://nats:4222}", name
        assert service["depends_on"]["nats"]["condition"] == "service_healthy", name


@pytest.mark.feature("compose-stack-own-bus")
@pytest.mark.parametrize(
    "compose_file",
    sorted(
        str(path.relative_to(ROOT))
        for path in [ROOT / "compose.yaml", *(ROOT / "scripts/swarm/stack").rglob("*.yaml")]
        if path.is_file()
    ),
)
def test_the_stack_never_reads_the_bus_settings_of_the_shell_that_runs_compose(compose_file: str) -> None:
    text = (ROOT / compose_file).read_text()
    assert "${AGENTIHOOKS_BUS" not in text, compose_file


@pytest.mark.feature("compose-stack-own-bus")
def test_an_agent_shell_on_the_anton_bus_leaves_the_controller_on_the_stack_nats() -> None:
    shell = {
        "AGENTIHOOKS_BUS": "memory",
        "AGENTIHOOKS_BUS_URL": "tls://swarm-internal.homeofanton.com:4222",
        "AGENTIHOOKS_BUS_CREDENTIALS": "/home/agent/.agentihooks/hive/bus.creds",
    }
    environment = SERVICES["controller"]["environment"]
    resolved = {key: _interpolate(str(environment[key]), shell) for key in ("AGENTIHOOKS_BUS", "AGENTIHOOKS_BUS_URL")}
    assert resolved == {"AGENTIHOOKS_BUS": "nats", "AGENTIHOOKS_BUS_URL": "nats://nats:4222"}
    assert "AGENTIHOOKS_BUS_CREDENTIALS" not in environment


@pytest.mark.feature("compose-stack-own-modes")
@pytest.mark.parametrize(
    "compose_file",
    sorted(
        str(path.relative_to(ROOT))
        for path in [ROOT / "compose.yaml", *(ROOT / "scripts/swarm/stack").rglob("*.yaml")]
        if path.is_file()
    ),
)
def test_the_stack_never_reads_the_deployment_or_scheduler_backend_of_the_shell(compose_file: str) -> None:
    text = (ROOT / compose_file).read_text()
    assert not re.search(r"(?<!\$)\$\{?AGENTIHOOKS_(DEPLOYMENT|SCHEDULER_STATE_BACKEND)\b", text), compose_file


@pytest.mark.feature("compose-stack-own-modes")
@pytest.mark.parametrize(
    ("service", "deployment"),
    [("swarm", "local"), ("api", "local"), ("controller", "compose"), ("hive", "compose")],
)
def test_the_stack_renders_one_deployment_and_scheduler_backend_from_any_shell(service: str, deployment: str) -> None:
    keys = ("AGENTIHOOKS_DEPLOYMENT", "AGENTIHOOKS_SCHEDULER_STATE_BACKEND")
    environment = SERVICES[service]["environment"]
    agent = {"AGENTIHOOKS_DEPLOYMENT": "distributed", "AGENTIHOOKS_SCHEDULER_STATE_BACKEND": "redis"}
    unset = {key: _interpolate(str(environment[key]), {}) for key in keys}
    from_agent = {key: _interpolate(str(environment[key]), agent) for key in keys}
    assert unset == from_agent == {"AGENTIHOOKS_DEPLOYMENT": deployment, "AGENTIHOOKS_SCHEDULER_STATE_BACKEND": "bus"}
    stack = {"SWARM_DEPLOYMENT": "local", "SWARM_SCHEDULER_STATE_BACKEND": "redis"}
    assert {key: _interpolate(str(environment[key]), stack) for key in keys} == {
        "AGENTIHOOKS_DEPLOYMENT": "local",
        "AGENTIHOOKS_SCHEDULER_STATE_BACKEND": "redis",
    }


def _interpolate(value: str, shell: dict[str, str]) -> str:
    """Resolve a Compose ${NAME:-default} value the way Compose does against the calling shell."""
    match = re.fullmatch(r"\$\{(\w+):-([^}]*)\}", value)
    if not match:
        return value
    return shell.get(match.group(1)) or match.group(2)


def test_swarm_init_smoke_pulls_nats_and_reads_the_four_service_ready_line():
    smoke = (ROOT / "scripts/packaging/swarm-init-smoke.sh").read_text()
    pull = "bash scripts/packaging/pull-runtime-image.sh mirror.gcr.io/library/nats:2.15.1-alpine\n"
    assert pull in smoke
    assert smoke.index(pull) < smoke.index('"$work/venv/bin/agentihooks" swarm init')
    assert "grep -qx 'Swarm stack ready: redis, nats, swarm and controller are healthy.'" in smoke


def test_the_api_service_runs_the_write_process_against_the_shared_database() -> None:
    api = SERVICES["api"]
    assert api["command"] == ["python", "-m", "scripts.swarm_ledger.api_service", "--serve"]
    assert api["environment"]["LEDGER_API_HOST"] == "0.0.0.0"
    assert api["environment"]["LEDGER_API_PORT"] == "8766"
    assert api["environment"]["AGENTIHOOKS_SWARM_DATABASE_URL"] == "${AGENTIHOOKS_SWARM_DATABASE_URL:-}"
    assert "ports" not in api
    assert api["depends_on"]["redis"]["condition"] == "service_healthy"


def test_the_front_routes_reads_to_the_ledger_and_writes_to_the_api() -> None:
    front = SERVICES["front"]
    assert front["ports"] == ["${SWARM_BIND:-127.0.0.1}:${SWARM_PORT:-8765}:8765"]
    assert "ports" not in SERVICES["swarm"]
    assert front["depends_on"]["swarm"]["condition"] == "service_healthy"
    assert front["depends_on"]["api"]["condition"] == "service_healthy"
    conf = (ROOT / "deploy/compose/ledger-front.conf").read_text()
    assert "${SWARM_LEDGER_SCHEME}://swarm:8765" in conf
    assert "http://api:8766" in conf
    assert "GET|HEAD" in conf


def test_the_front_proxies_reads_with_the_scheme_the_swarm_serves() -> None:
    front = SERVICES["front"]
    assert front["environment"] == {"SWARM_LEDGER_SCHEME": "${SWARM_LEDGER_SCHEME:-http}"}
    assert front["volumes"] == ["./deploy/compose/ledger-front.conf:/etc/nginx/templates/default.conf.template:ro"]
    smoke = (ROOT / "scripts/packaging/stability-smoke.sh").read_text()
    override = yaml.safe_load(smoke.split("<<'YAML'\n", 1)[1].split("\nYAML\n", 1)[0])["services"]
    assert override["swarm"]["environment"]["LEDGER_TLS_CERT"] == "/tls/cert.pem"
    assert override["front"]["environment"] == {"SWARM_LEDGER_SCHEME": "https"}
    read = smoke.split("if ! python3 - \"$slug\" <<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    assert "http://127.0.0.1:{os.environ['SWARM_PORT']}" in read
    assert 'for route in ("/healthz", "/api/v1/ledgers", f"/{slug}"):' in read
    assert smoke.index(read) < smoke.index('docker run -d --name "$run-member"')


@pytest.mark.feature("compose-front-ready-before-wait")
@pytest.mark.parametrize("compose_file", ["compose.yaml", "scripts/swarm/stack/compose.yaml"])
def test_the_front_reads_healthy_only_once_nginx_serves_the_ledger_health_route(compose_file: str) -> None:
    front = yaml.safe_load((ROOT / compose_file).read_text())["services"]["front"]
    assert front["healthcheck"] == {
        "test": ["CMD", "wget", "-q", "-O-", "http://127.0.0.1:8765/healthz"],
        "interval": "5s",
        "timeout": "3s",
        "retries": 5,
        "start_period": "30s",
        "start_interval": "1s",
    }


@pytest.mark.feature("compose-api-health-and-front-proxy")
@pytest.mark.parametrize(
    "conf",
    [
        "deploy/compose/ledger-front.conf",
        "scripts/swarm/stack/deploy/compose/ledger-front.conf",
        "deploy/compose/ledger-local-front.conf.template",
    ],
)
def test_the_front_forwards_the_host_the_client_sent_with_its_port(conf: str) -> None:
    text = (ROOT / conf).read_text()
    assert "proxy_set_header Host $http_host;" in text
    assert "$host;" not in text


@pytest.mark.feature("compose-api-health-and-front-proxy")
@pytest.mark.parametrize(
    "conf", ["deploy/compose/ledger-front.conf", "scripts/swarm/stack/deploy/compose/ledger-front.conf"]
)
def test_the_front_resolves_the_swarm_and_api_names_through_the_compose_dns_at_request_time(conf: str) -> None:
    text = (ROOT / conf).read_text()
    assert "set $upstream ${SWARM_LEDGER_SCHEME}://swarm:8765;" in text
    assert "set $upstream http://api:8766;" in text
    assert "resolver 127.0.0.11 valid=1s;" in text
