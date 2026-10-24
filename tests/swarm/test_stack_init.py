import io
import json
import shlex
import stat
import subprocess
import urllib.error
from pathlib import Path

import pytest

from scripts.swarm import stack_env, stack_init
from scripts.swarm_ledger import server_compose
from tests.swarm.compose_loader import compose_file

PASSWORD = "pw-test"
HEALTHY = [
    {"Service": "redis", "State": "running", "Health": "healthy"},
    {"Service": "swarm", "State": "running", "Health": "healthy"},
    {"Service": "controller", "State": "running", "Health": "healthy"},
    {"Service": "nats", "State": "running", "Health": "healthy"},
]
STACK_FILES = [
    "compose.yaml",
    "deploy/compose/adopt.yaml",
    "deploy/compose/redis/healthcheck.sh",
    "deploy/compose/redis/start.sh",
    "deploy/compose/redis/users.acl.template",
]


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def page(url, timeout):
    assert timeout == 5
    return Response(b"ok" if url.endswith("/healthz") else b"<!doctype html><title>HOME</title>")


class Docker:
    def __init__(self, rows=HEALTHY, failing=(), up_code=0, up_err="", ps_text=None, ps_err=""):
        self.calls = []
        self.rows, self.failing, self.up_code, self.up_err = rows, failing, up_code, up_err
        self.ps_text, self.ps_err = ps_text, ps_err

    def __call__(self, argv, capture_output, text):
        assert capture_output is True and text is True
        self.calls.append(argv)
        if argv[1:3] in self.failing:
            return subprocess.CompletedProcess(argv, 1, "", "no")
        if "up" in argv:
            return subprocess.CompletedProcess(argv, self.up_code, "", self.up_err)
        if "ps" in argv:
            if self.ps_err:
                return subprocess.CompletedProcess(argv, 1, "", self.ps_err)
            out = self.ps_text if self.ps_text is not None else "\n".join(json.dumps(row) for row in self.rows)
            return subprocess.CompletedProcess(argv, 0, out, "")
        return subprocess.CompletedProcess(argv, 0, "", "")


class Terminal(io.StringIO):
    def isatty(self):
        return True


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(stack_init.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(stack_env.secrets, "token_urlsafe", lambda: PASSWORD)
    monkeypatch.setattr(stack_env, "free_port", lambda preferred: preferred + 1)
    return tmp_path


def init(home, docker, argv=(), opener=page, environ=None):
    out = io.StringIO()
    code = stack_init.init(list(argv), environ or {"AGENTIHOOKS_HOME": str(home)}, docker, opener, out, "demo-swarm")
    return code, out.getvalue()


def stack(home):
    return home / "swarm-stack"


def compose(home, *args):
    folder = stack(home)
    return [
        "docker",
        "compose",
        "--project-name",
        "agentihooks-swarm",
        "--project-directory",
        str(folder),
        "--file",
        str(folder / "compose.yaml"),
        "--env-file",
        str(folder / ".env"),
        *args,
    ]


def adopted(home, *args):
    argv = compose(home, *args)
    at = argv.index("--env-file")
    return [*argv[:at], "--file", str(stack(home) / "deploy" / "compose" / "adopt.yaml"), *argv[at:]]


def up(home, timeout):
    return compose(home, "up", "--detach", "--no-build", "--wait", "--wait-timeout", timeout)


READY = (
    "Starting the swarm stack; the first run pulls its images.\n"
    "Swarm stack ready: redis, nats, swarm and controller are healthy.\n"
    "Next steps:\n"
    "  1. Open the ledger: http://127.0.0.1:8766/\n"
    "  2. Point this shell at the stack: set -a; . {folder}/client.env; set +a\n"
    "  3. Start the swarm only when you want it: agentihooks swarm demo-swarm start\n"
    "Running agentihooks swarm init again is safe: it keeps the password, the ports and the data.\n"
)


def test_init_brings_up_the_stack_and_prints_the_next_steps(home):
    docker = Docker()
    code, out = init(home, docker)
    assert code == 0
    assert out == READY.format(folder=stack(home))
    assert docker.calls == [
        ["docker", "info", "--format", "{{.ServerVersion}}"],
        ["docker", "compose", "version", "--short"],
        up(home, "300"),
        compose(home, "ps", "--all", "--format", "json"),
    ]
    folder = stack(home)
    assert f"SWARM_REDIS_PASSWORD={PASSWORD}\n" in (folder / ".env").read_text()
    assert (folder / "client.env").read_text() == stack_env.client_env(stack_env.read_env(folder / ".env"), folder)
    for name in (".env", "client.env"):
        assert stat.S_IMODE((folder / name).stat().st_mode) == 0o600
    for name in STACK_FILES:
        assert (folder / name).read_bytes() == (stack_init.BUNDLED / name).read_bytes(), name
    assert (folder / "deploy/compose/tls").is_dir()
    assert PASSWORD not in out


def test_a_second_run_keeps_the_password_ports_and_edits(home, monkeypatch):
    init(home, Docker())
    env = stack(home) / ".env"
    env.write_text(env.read_text().replace("SWARM_PORT=8766", "SWARM_PORT=9100"))
    monkeypatch.setattr(stack_env.secrets, "token_urlsafe", lambda: "other")
    docker = Docker()
    code, out = init(home, docker, ["--timeout", "42"])
    assert code == 0
    assert "SWARM_PORT=9100\n" in env.read_text()
    assert f"SWARM_REDIS_PASSWORD={PASSWORD}\n" in env.read_text()
    assert "  1. Open the ledger: http://127.0.0.1:9100/\n" in out
    assert (stack(home) / "client.env").read_text().splitlines()[1] == "LEDGER_URL=http://127.0.0.1:9100"
    assert docker.calls[2] == up(home, "42")


ADOPTED_REDIS = "redis://swarm:" + "pw~x" + "@127.0.0.1:6379/0"
ADOPTED = {
    "SWARM_ADOPT_LEDGER_DIR": "/srv/ledger",
    "SWARM_ADOPT_HOME": "/home/op",
    "SWARM_ADOPT_UID": "1234",
    "SWARM_ADOPT_GID": "5678",
    "SWARM_ADOPT_CODE": str(server_compose.ROOT),
    "SWARM_ADOPT_REDIS_URL": ADOPTED_REDIS,
    "SWARM_PORT": "8765",
}


def prepared(home):
    (home / "swarm-client.env").write_text(f"AGENTIHOOKS_SWARM_REDIS_URL={shlex.quote(ADOPTED_REDIS)}\n")


def test_adopt_records_the_ledger_folder_user_home_and_port_and_keeps_them(home, monkeypatch):
    monkeypatch.setattr(stack_init.Path, "home", lambda: Path("/home/op"))
    monkeypatch.setattr(stack_init.os, "getuid", lambda: 1234)
    monkeypatch.setattr(stack_init.os, "getgid", lambda: 5678)
    init(home, Docker())
    prepared(home)
    docker = Docker()
    environ = {"AGENTIHOOKS_HOME": str(home), "LEDGER_DIR": "/srv/ledger"}
    code, out = init(home, docker, ["--adopt"], environ=environ)
    env = stack(home) / ".env"
    values = stack_env.read_env(env)
    assert code == 0
    assert {key: values[key] for key in ADOPTED} == ADOPTED
    assert values["SWARM_REDIS_PASSWORD"] == PASSWORD
    assert values["SWARM_REDIS_PORT"] == "6380"
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert docker.calls[2:] == [
        adopted(home, "up", "--detach", "--no-build", "--wait", "--wait-timeout", "300"),
        adopted(home, "ps", "--all", "--format", "json"),
    ]
    assert "  1. Open the ledger: http://127.0.0.1:8765/\n" in out
    again = Docker()
    assert init(home, again, environ={"AGENTIHOOKS_HOME": str(home), "LEDGER_DIR": "/other"})[0] == 0
    assert again.calls[2] == adopted(home, "up", "--detach", "--no-build", "--wait", "--wait-timeout", "300")
    assert {key: stack_env.read_env(env)[key] for key in ADOPTED} == ADOPTED


def test_adopt_defaults_to_the_shared_ledger_folder_in_the_home(home, monkeypatch):
    monkeypatch.setattr(stack_init.Path, "home", lambda: Path("/home/op"))
    prepared(home)
    code, _ = init(home, Docker(), ["--adopt"])
    assert code == 0
    values = stack_env.read_env(stack(home) / ".env")
    assert values["SWARM_ADOPT_LEDGER_DIR"] == "/home/op/development-ledger"
    assert values["SWARM_ADOPT_HOME"] == "/home/op"


def test_adopt_and_prepare_clients_are_refused_together(home, capsys):
    docker = Docker()
    with pytest.raises(SystemExit) as raised:
        init(home, docker, ["--prepare-clients", "--adopt"])
    assert raised.value.code == 2
    assert "argument --adopt: not allowed with argument --prepare-clients" in capsys.readouterr().err
    assert docker.calls == []
    assert not stack(home).exists()


def test_no_start_records_adoption_and_starts_no_service(home, monkeypatch):
    monkeypatch.setattr(stack_init.Path, "home", lambda: Path("/home/op"))
    prepared(home)
    docker = Docker()
    code, out = init(home, docker, ["--adopt", "--no-start"])
    folder = stack(home)
    assert (code, out) == (0, f"Swarm stack settings written to {folder}; no service was started.\n")
    assert docker.calls == [
        ["docker", "info", "--format", "{{.ServerVersion}}"],
        ["docker", "compose", "version", "--short"],
    ]
    values = stack_env.read_env(folder / ".env")
    assert values["SWARM_ADOPT_LEDGER_DIR"] == "/home/op/development-ledger"
    assert values["SWARM_PORT"] == "8765"
    assert (folder / "client.env").read_text() == stack_env.client_env(values, folder)
    for name in STACK_FILES:
        assert (folder / name).read_bytes() == (stack_init.BUNDLED / name).read_bytes(), name


def test_the_adoption_override_serves_the_host_folder_as_the_host_user():
    swarm = compose_file(stack_init.BUNDLED / "deploy" / "compose" / "adopt.yaml")["services"]["swarm"]
    assert (
        swarm["user"]
        == "${SWARM_ADOPT_UID:?run agentihooks swarm init --adopt}:${SWARM_ADOPT_GID:?run agentihooks swarm init --adopt}"
    )
    assert swarm["pid"] == "host"
    assert swarm["network_mode"] == "host"
    assert swarm["ports"] == swarm["depends_on"] == "!reset"
    assert "image" not in swarm
    assert (swarm["mem_limit"], swarm["memswap_limit"], swarm["cpus"], swarm["pids_limit"]) == (
        "1536m",
        "1536m",
        2,
        200,
    )
    assert swarm["command"] == [
        "python",
        "${SWARM_ADOPT_CODE:?run agentihooks swarm init --adopt}/scripts/swarm_ledger/server_supervisor.py",
    ]
    assert swarm["working_dir"] == "${SWARM_ADOPT_CODE}"
    assert swarm["environment"] == {
        "HOME": "${SWARM_ADOPT_HOME}",
        "LEDGER_DIR": "${SWARM_ADOPT_LEDGER_DIR}",
        "LEDGER_HOST": "127.0.0.1",
        "LEDGER_TLS_CERT": "${LEDGER_TLS_CERT:-}",
        "LEDGER_TLS_KEY": "${LEDGER_TLS_KEY:-}",
        "PYTHONPATH": "${SWARM_ADOPT_CODE}",
        "SWARM_RELOAD": "1",
        "MALLOC_ARENA_MAX": "2",
        "AGENTIHOOKS_SWARM_REDIS_URL": "${SWARM_ADOPT_REDIS_URL:?run agentihooks swarm init --adopt}",
    }
    assert swarm["volumes"] == [
        "${SWARM_ADOPT_LEDGER_DIR}:${SWARM_ADOPT_LEDGER_DIR}",
        "${SWARM_ADOPT_HOME}/.agentihooks:${SWARM_ADOPT_HOME}/.agentihooks",
        "${SWARM_ADOPT_CODE}:${SWARM_ADOPT_CODE}:ro",
    ]


def test_adopt_refuses_without_a_prepared_client_and_records_nothing(home):
    docker = Docker()
    code, out = init(home, docker, ["--adopt", "--no-start"])
    assert code == 1
    assert out == (
        "Swarm stack not ready: no prepared swarm Redis client to adopt.\n"
        "  Fix: run agentihooks swarm init --prepare-clients, then agentihooks swarm init --adopt.\n"
    )
    assert "SWARM_ADOPT_LEDGER_DIR" not in stack_env.read_env(stack(home) / ".env")
    assert docker.calls == [
        ["docker", "info", "--format", "{{.ServerVersion}}"],
        ["docker", "compose", "version", "--short"],
    ]
    (home / "swarm-client.env").write_text("OTHER=1\n")
    assert init(home, Docker(), ["--adopt", "--no-start"])[0] == 1
    assert "SWARM_ADOPT_LEDGER_DIR" not in stack_env.read_env(stack(home) / ".env")


def test_adopted_client_settings_name_the_adopted_redis(home, monkeypatch):
    monkeypatch.setattr(stack_init.Path, "home", lambda: Path("/home/op"))
    prepared(home)
    assert init(home, Docker(), ["--adopt", "--no-start"])[0] == 0
    client = stack_env.read_env(stack(home) / "client.env")
    assert client["AGENTIHOOKS_SWARM_REDIS_URL"] == shlex.quote(ADOPTED_REDIS)
    assert shlex.split(client["AGENTIHOOKS_SWARM_REDIS_URL"]) == [ADOPTED_REDIS]
    assert client["LEDGER_URL"] == "http://127.0.0.1:8765"


def test_an_unhealthy_adopted_ledger_names_the_legacy_ledger_to_stop(home):
    prepared(home)
    rows = [HEALTHY[0], {"Service": "swarm", "State": "running", "Health": "unhealthy"}, *HEALTHY[2:]]
    code, out = init(home, Docker(rows=rows, up_code=1), ["--adopt"])
    assert code == 1
    assert out.endswith(
        "Swarm stack not ready: swarm is unhealthy.\n"
        "  Fix: read its logs; the adopted ledger server serves port 8765 on the host network, "
        "so stop the legacy ledger container by its id first, then run agentihooks swarm init again.\n"
        "  Logs: docker compose --project-name agentihooks-swarm logs swarm\n"
    )


def test_stack_folder_defaults_to_the_agentihooks_home(monkeypatch, tmp_path):
    monkeypatch.setattr(stack_init.Path, "home", lambda: tmp_path)
    assert stack_init.stack_home({}) == tmp_path / ".agentihooks" / "swarm-stack"
    assert stack_init.stack_home({"AGENTIHOOKS_HOME": "/x"}) == Path("/x/swarm-stack")


DAEMON = (
    "Swarm stack not ready: the Docker daemon is not reachable.\n"
    "  Fix: start Docker, or add your user to the docker group, then run agentihooks swarm init again.\n"
)


@pytest.mark.parametrize(
    ("which", "failing", "message"),
    [
        (
            None,
            (),
            "Swarm stack not ready: Docker is not installed.\n"
            "  Fix: install Docker Engine with the Compose plugin, then run agentihooks swarm init again.\n",
        ),
        ("/usr/bin/docker", (["info", "--format"],), DAEMON),
        ("/usr/bin/docker", (["info", "--format"], ["compose", "version"]), DAEMON),
        (
            "/usr/bin/docker",
            (["compose", "version"],),
            "Swarm stack not ready: the Docker Compose plugin is missing.\n"
            "  Fix: install Docker Compose v2, the docker compose plugin, then run agentihooks swarm init again.\n",
        ),
    ],
)
def test_a_missing_docker_piece_is_named_with_its_fix(home, monkeypatch, which, failing, message):
    seen = []
    monkeypatch.setattr(stack_init.shutil, "which", lambda name: seen.append(name) or which)
    docker = Docker(failing=failing)
    code, out = init(home, docker)
    assert (code, out) == (1, message)
    assert seen == ["docker"]
    assert not any("up" in call for call in docker.calls)
    assert not stack(home).exists()


def test_an_unhealthy_service_is_named_with_its_fix_and_logs(home):
    rows = [HEALTHY[0], {"Service": "swarm", "State": "running", "Health": "unhealthy"}]
    docker = Docker(
        rows=rows, up_code=1, up_err="line one\nBind for 127.0.0.1:6380 failed: port is already allocated\n"
    )
    code, out = init(home, docker)
    env = stack(home) / ".env"
    assert code == 1
    assert out == (
        "Starting the swarm stack; the first run pulls its images.\n"
        "docker compose said:\n"
        "  line one\n"
        "  Bind for 127.0.0.1:6380 failed: port is already allocated\n"
        "Swarm stack not ready: nats is not created.\n"
        "  Fix: read its logs; NATS keeps its JetStream store on the nats-data volume, so free space there "
        "or remove that volume to start the bus empty, then run agentihooks swarm init again.\n"
        "  Logs: docker compose --project-name agentihooks-swarm logs nats\n"
        "Swarm stack not ready: swarm is unhealthy.\n"
        "  Fix: read its logs; the ledger server needs Redis healthy, and if port 8766 is in use, "
        f"set SWARM_PORT in {env} to a free port, then run agentihooks swarm init again.\n"
        "  Logs: docker compose --project-name agentihooks-swarm logs swarm\n"
        "Swarm stack not ready: controller is not created.\n"
        "  Fix: read its logs; the controller starts once Redis and the ledger server are healthy "
        "and then holds a lease, so fix those first, then run agentihooks swarm init again.\n"
        "  Logs: docker compose --project-name agentihooks-swarm logs controller\n"
    )


def test_compose_output_is_trimmed_to_its_last_lines(home):
    lines = [f"line {n}" for n in range(15)]
    code, out = init(home, Docker(rows=[], up_code=1, up_err="\n".join(lines)))
    assert code == 1
    assert "docker compose said:\n" + "".join(f"  {line}\n" for line in lines[5:]) in out
    assert "  line 4\n" not in out


def test_a_failed_listing_is_shown_and_every_service_state_is_unknown(home):
    code, out = init(home, Docker(ps_err="cannot connect\n"))
    assert code == 1
    assert "docker compose said:\n  cannot connect\nSwarm stack not ready: redis is in an unknown state.\n" in out
    assert "Swarm stack not ready: controller is in an unknown state.\n" in out


def test_an_unhealthy_redis_names_its_port_and_password(home):
    docker = Docker(rows=[{"Service": "redis", "State": "exited", "Health": ""}], up_code=1)
    code, out = init(home, docker)
    env = stack(home) / ".env"
    assert code == 1
    assert (
        "Swarm stack not ready: redis is exited.\n"
        f"  Fix: read its logs; if port 6380 is in use, set SWARM_REDIS_PORT in {env} to a free port, "
        f"and keep the password in {env} as it was at the first start, then run agentihooks swarm init again.\n"
        "  Logs: docker compose --project-name agentihooks-swarm logs redis\n"
    ) in out
    assert "docker compose said:" not in out


def test_an_unhealthy_nats_holds_the_stack_back_with_its_own_fix(home):
    rows = [*HEALTHY[:3], {"Service": "nats", "State": "running", "Health": "unhealthy"}]
    code, out = init(home, Docker(rows=rows, up_code=1))
    assert code == 1
    assert out == (
        "Starting the swarm stack; the first run pulls its images.\n"
        "Swarm stack not ready: nats is unhealthy.\n"
        "  Fix: read its logs; NATS keeps its JetStream store on the nats-data volume, so free space there "
        "or remove that volume to start the bus empty, then run agentihooks swarm init again.\n"
        "  Logs: docker compose --project-name agentihooks-swarm logs nats\n"
    )


def test_a_failed_up_with_healthy_services_is_still_ready(home):
    code, out = init(home, Docker(up_code=1, up_err="warning"))
    assert code == 0
    assert "docker compose said:" not in out


def test_compose_ps_as_a_json_array_is_read(home):
    code, _ = init(home, Docker(ps_text=json.dumps(HEALTHY)))
    assert code == 0


def test_an_unanswered_ledger_page_is_reported(home):
    def refused(url, timeout):
        raise urllib.error.URLError("refused")

    code, out = init(home, Docker(), opener=refused)
    assert code == 1
    assert out.endswith(
        "Swarm stack not ready: the ledger page does not answer.\n"
        "  Fix: http://127.0.0.1:8766/healthz did not answer (<urlopen error refused>); "
        "read the ledger logs with docker compose --project-name agentihooks-swarm logs swarm, "
        "then run agentihooks swarm init again.\n"
    )


def test_a_page_that_is_not_the_ledger_home_is_reported(home):
    def other(url, timeout):
        return Response(b"ok" if url.endswith("/healthz") else b"<title>nginx</title>")

    code, out = init(home, Docker(), opener=other)
    assert code == 1
    assert out.endswith(
        "Swarm stack not ready: the ledger page does not answer.\n"
        "  Fix: http://127.0.0.1:8766/ did not serve the ledger home page; "
        "read the ledger logs with docker compose --project-name agentihooks-swarm logs swarm, "
        "then run agentihooks swarm init again.\n"
    )


def test_colour_on_a_terminal_and_never_with_no_color(home):
    out = Terminal()
    stack_init.init([], {"AGENTIHOOKS_HOME": str(home)}, Docker(), page, out, "demo-swarm")
    assert "\033[1;32mSwarm stack ready: redis, nats, swarm and controller are healthy.\033[0m\n" in out.getvalue()
    assert "  1. Open the ledger: \033[36mhttp://127.0.0.1:8766/\033[0m\n" in out.getvalue()
    plain = Terminal()
    stack_init.init([], {"AGENTIHOOKS_HOME": str(home), "NO_COLOR": "1"}, Docker(), page, plain, "demo-swarm")
    assert "\033[" not in plain.getvalue()


def test_failures_are_red_on_a_terminal(home, monkeypatch):
    monkeypatch.setattr(stack_init.shutil, "which", lambda name: None)
    out = Terminal()
    stack_init.init([], {"AGENTIHOOKS_HOME": str(home)}, Docker(), page, out, "demo-swarm")
    assert out.getvalue() == (
        "\033[1;31mSwarm stack not ready: Docker is not installed.\033[0m\n"
        "  Fix: install Docker Engine with the Compose plugin, then run agentihooks swarm init again.\n"
    )


def test_timeout_must_be_a_number(home, capsys):
    with pytest.raises(SystemExit) as raised:
        init(home, Docker(), ["--timeout", "soon"])
    assert raised.value.code == 2
    assert "invalid int value: 'soon'" in capsys.readouterr().err


def test_help_names_the_command_and_its_timeout():
    text = " ".join(stack_init.parser().format_help().split())
    description = " ".join(stack_init.__doc__.split())
    assert text == (
        f"usage: agentihooks swarm init [-h] [--timeout TIMEOUT] [--prepare-clients | --adopt] [--no-start] {description} options: "
        "-h, --help show this help message and exit "
        "--timeout TIMEOUT seconds to wait for every service to be healthy "
        "--prepare-clients verify bundle credentials on existing local Redis and activate clients "
        "--adopt serve the existing shared ledger folder as this user from the stack, on the default ledger port, "
        "and keep doing so on every later run "
        "--no-start write the stack files and settings, adoption included, without starting any service"
    )


def test_a_tls_stack_points_the_client_at_its_own_ca(home):
    folder = stack(home)
    folder.mkdir()
    (folder / ".env").write_text("SWARM_REDIS_SCHEME=rediss\n")
    code, _ = init(home, Docker())
    assert code == 0
    assert (folder / "client.env").read_text().endswith(f"\nSSL_CERT_FILE={folder}/deploy/compose/tls/ca.pem\n")


def test_a_stack_env_can_name_its_own_compose_project_so_a_second_stack_never_touches_the_first(home):
    folder = stack(home)
    folder.mkdir()
    (folder / ".env").write_text("SWARM_COMPOSE_PROJECT=agentihooks-swarm-proof\n")
    docker = Docker(rows=HEALTHY[:2])
    code, out = init(home, docker)
    assert code == 1
    assert {call[3] for call in docker.calls if call[1:3] == ["compose", "--project-name"]} == {
        "agentihooks-swarm-proof"
    }
    assert "  Logs: docker compose --project-name agentihooks-swarm-proof logs controller\n" in out
