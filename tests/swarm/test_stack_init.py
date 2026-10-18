import io
import json
import socket
import stat
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from scripts.swarm import cli, stack_init

PASSWORD = "pw-test"
CLIENT_REDIS = "redis://" + "swarm:" + PASSWORD + "@127.0.0.1:6380/0"
HEALTHY = [
    {"Service": "redis", "State": "running", "Health": "healthy"},
    {"Service": "swarm", "State": "running", "Health": "healthy"},
    {"Service": "controller", "State": "running", "Health": "healthy"},
]
STACK_FILES = [
    "compose.yaml",
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
    def __init__(self, rows=HEALTHY, failing=(), up_code=0, up_err="", ps_text=None):
        self.calls = []
        self.rows, self.failing, self.up_code, self.up_err, self.ps_text = rows, failing, up_code, up_err, ps_text

    def __call__(self, argv, capture_output, text):
        assert capture_output is True and text is True
        self.calls.append(argv)
        if argv[1:3] in self.failing:
            return subprocess.CompletedProcess(argv, 1, "", "no")
        if "up" in argv:
            return subprocess.CompletedProcess(argv, self.up_code, "", self.up_err)
        if "ps" in argv:
            out = self.ps_text if self.ps_text is not None else "\n".join(json.dumps(row) for row in self.rows)
            return subprocess.CompletedProcess(argv, 0, out, "")
        return subprocess.CompletedProcess(argv, 0, "", "")


class Terminal(io.StringIO):
    def isatty(self):
        return True


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(stack_init.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(stack_init.secrets, "token_urlsafe", lambda: PASSWORD)
    monkeypatch.setattr(stack_init, "free_port", lambda preferred: preferred + 1)
    return tmp_path


def init(home, docker, argv=(), opener=page, environ=None):
    out = io.StringIO()
    code = stack_init.init(list(argv), environ or {"AGENTIHOOKS_HOME": str(home)}, docker, opener, out)
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


READY = (
    "Starting the swarm stack; the first run pulls its images.\n"
    "Swarm stack ready: redis, swarm and controller are healthy.\n"
    "Next steps:\n"
    "  1. Open the ledger: http://127.0.0.1:8766/\n"
    "  2. Point this shell at the stack: set -a; . {folder}/client.env; set +a\n"
    "  3. Start a swarm only when you want one: agentihooks swarm <id> create --repo <path>, "
    "then agentihooks swarm <id> start\n"
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
        compose(home, "up", "--detach", "--no-build", "--wait", "--wait-timeout", "300"),
        compose(home, "ps", "--all", "--format", "json"),
    ]
    folder = stack(home)
    assert (folder / ".env").read_text() == (
        "AGENTIHOOKS_DEPLOYMENT=compose\n"
        "SWARM_BIND=127.0.0.1\n"
        "SWARM_HIVE_ID=compose-controller\n"
        f"SWARM_REDIS_PASSWORD={PASSWORD}\n"
        "SWARM_PORT=8766\n"
        "SWARM_REDIS_PORT=6380\n"
    )
    assert (folder / "client.env").read_text() == (
        f"AGENTIHOOKS_DEPLOYMENT=compose\nLEDGER_URL=http://127.0.0.1:8766\nAGENTIHOOKS_SWARM_REDIS_URL={CLIENT_REDIS}\n"
    )
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
    env.chmod(0o644)
    monkeypatch.setattr(stack_init.secrets, "token_urlsafe", lambda: "other")
    monkeypatch.setattr(stack_init, "free_port", lambda preferred: pytest.fail("ports are chosen once"))
    docker = Docker()
    code, out = init(home, docker, ["--timeout", "42"])
    assert code == 0
    assert "SWARM_PORT=9100\n" in env.read_text()
    assert f"SWARM_REDIS_PASSWORD={PASSWORD}\n" in env.read_text()
    assert "  1. Open the ledger: http://127.0.0.1:9100/\n" in out
    assert (stack(home) / "client.env").read_text().splitlines()[1] == "LEDGER_URL=http://127.0.0.1:9100"
    assert stat.S_IMODE(env.stat().st_mode) == 0o644
    assert docker.calls[2] == compose(home, "up", "--detach", "--no-build", "--wait", "--wait-timeout", "42")


def test_missing_settings_are_filled_in_on_a_rerun(home):
    folder = stack(home)
    folder.mkdir()
    (folder / ".env").write_text("# SWARM_HIVE_ID=old\nSWARM_PORT = 9200\nSWARM_REDIS_PASSWORD=\n")
    init(home, Docker())
    assert (folder / ".env").read_text() == (
        "SWARM_PORT=9200\n"
        f"SWARM_REDIS_PASSWORD={PASSWORD}\n"
        "AGENTIHOOKS_DEPLOYMENT=compose\n"
        "SWARM_BIND=127.0.0.1\n"
        "SWARM_HIVE_ID=compose-controller\n"
        "SWARM_REDIS_PORT=6380\n"
    )


def test_stack_folder_defaults_to_the_agentihooks_home(monkeypatch, tmp_path):
    monkeypatch.setattr(stack_init.Path, "home", lambda: tmp_path)
    assert stack_init.stack_home({}) == tmp_path / ".agentihooks" / "swarm-stack"
    assert stack_init.stack_home({"AGENTIHOOKS_HOME": "/x"}) == Path("/x/swarm-stack")


@pytest.mark.parametrize(
    ("which", "failing", "message"),
    [
        (
            None,
            (),
            "Swarm stack not ready: Docker is not installed.\n"
            "  Fix: install Docker Engine with the Compose plugin, then run agentihooks swarm init again.\n",
        ),
        (
            "/usr/bin/docker",
            (["info", "--format"],),
            "Swarm stack not ready: the Docker daemon is not reachable.\n"
            "  Fix: start Docker, or add your user to the docker group, then run agentihooks swarm init again.\n",
        ),
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
        "docker compose up said:\n"
        "  line one\n"
        "  Bind for 127.0.0.1:6380 failed: port is already allocated\n"
        "Swarm stack not ready: swarm is unhealthy.\n"
        f"  Fix: the ledger server needs port 8766 free and Redis healthy; free the port or set SWARM_PORT in {env} "
        "to a free port, then run agentihooks swarm init again.\n"
        "  Logs: docker compose --project-name agentihooks-swarm logs swarm\n"
        "Swarm stack not ready: controller is not created.\n"
        "  Fix: the controller starts once Redis and the ledger server are healthy and then holds a lease; "
        "fix those first, then run agentihooks swarm init again.\n"
        "  Logs: docker compose --project-name agentihooks-swarm logs controller\n"
    )


def test_compose_output_is_trimmed_to_its_last_lines(home):
    lines = [f"line {n}" for n in range(15)]
    code, out = init(home, Docker(rows=[], up_code=1, up_err="\n".join(lines)))
    assert code == 1
    assert "docker compose up said:\n" + "".join(f"  {line}\n" for line in lines[5:]) in out
    assert "  line 4\n" not in out


def test_an_unhealthy_redis_names_its_port_and_password(home):
    docker = Docker(rows=[{"Service": "redis", "State": "exited", "Health": ""}], up_code=1)
    code, out = init(home, docker)
    env = stack(home) / ".env"
    assert code == 1
    assert (
        "Swarm stack not ready: redis is exited.\n"
        f"  Fix: Redis needs port 6380 free and the password in {env} unchanged since its first start; "
        "free the port or set SWARM_REDIS_PORT to a free port, then run agentihooks swarm init again.\n"
        "  Logs: docker compose --project-name agentihooks-swarm logs redis\n"
    ) in out
    assert "docker compose up said:" not in out


def test_a_failed_up_with_healthy_services_is_still_ready(home):
    code, out = init(home, Docker(up_code=1, up_err="warning"))
    assert code == 0
    assert "docker compose up said:" not in out


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
    stack_init.init([], {"AGENTIHOOKS_HOME": str(home)}, Docker(), page, out)
    assert "\033[1;32mSwarm stack ready: redis, swarm and controller are healthy.\033[0m\n" in out.getvalue()
    assert "  1. Open the ledger: \033[36mhttp://127.0.0.1:8766/\033[0m\n" in out.getvalue()
    plain = Terminal()
    stack_init.init([], {"AGENTIHOOKS_HOME": str(home), "NO_COLOR": "1"}, Docker(), page, plain)
    assert "\033[" not in plain.getvalue()


def test_failures_are_red_on_a_terminal(home, monkeypatch):
    monkeypatch.setattr(stack_init.shutil, "which", lambda name: None)
    out = Terminal()
    stack_init.init([], {"AGENTIHOOKS_HOME": str(home)}, Docker(), page, out)
    assert out.getvalue() == (
        "\033[1;31mSwarm stack not ready: Docker is not installed.\033[0m\n"
        "  Fix: install Docker Engine with the Compose plugin, then run agentihooks swarm init again.\n"
    )


def test_free_port_prefers_the_default_and_falls_back_to_any_free_port():
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        held.listen()
        busy = held.getsockname()[1]
        other = stack_init.free_port(busy)
        assert other not in (busy, 0)
    assert stack_init.free_port(busy) == busy


def test_swarm_init_routes_to_the_stack_command(monkeypatch):
    seen = []
    monkeypatch.setattr(stack_init, "main", lambda argv: seen.append(argv) or 7)
    assert cli.main(["init", "--timeout", "9"]) == 7
    assert seen == [["--timeout", "9"]]


def test_timeout_must_be_a_number(home, capsys):
    with pytest.raises(SystemExit) as raised:
        init(home, Docker(), ["--timeout", "soon"])
    assert raised.value.code == 2
    assert "invalid int value: 'soon'" in capsys.readouterr().err


def test_main_runs_init_with_the_process_environment_docker_and_stdout(monkeypatch):
    seen = []
    monkeypatch.setattr(stack_init, "init", lambda *args: seen.append(args) or 3)
    assert stack_init.main(["--timeout", "1"]) == 3
    assert seen == [(["--timeout", "1"], stack_init.os.environ, subprocess.run, urllib.request.urlopen, sys.stdout)]


def test_write_private_replaces_a_longer_file(tmp_path):
    path = tmp_path / "client.env"
    path.write_text("a much longer earlier text\n")
    stack_init.write_private(path, "short\n")
    assert path.read_text() == "short\n"
