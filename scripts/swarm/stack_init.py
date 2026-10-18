"""agentihooks swarm init: bring up the local swarm stack with Docker Compose and print the next steps.

It never creates or starts a swarm; the printed commands do that when the operator chooses to.
"""

import argparse
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

PROJECT = "agentihooks-swarm"
BUNDLED = Path(__file__).with_name("stack")
SERVICES = ("redis", "swarm", "controller")
BIND = "127.0.0.1"
AGAIN = "then run agentihooks swarm init again"
LOGS = f"docker compose --project-name {PROJECT} logs"
OUTPUT_LINES = 10
DOCKER_CHECKS = (
    (
        ["docker", "info", "--format", "{{.ServerVersion}}"],
        "the Docker daemon is not reachable",
        f"start Docker, or add your user to the docker group, {AGAIN}",
    ),
    (
        ["docker", "compose", "version", "--short"],
        "the Docker Compose plugin is missing",
        f"install Docker Compose v2, the docker compose plugin, {AGAIN}",
    ),
)
FIXES = {
    "redis": "Redis needs port {SWARM_REDIS_PORT} free and the password in {env} unchanged since its first start; "
    f"free the port or set SWARM_REDIS_PORT to a free port, {AGAIN}",
    "swarm": "the ledger server needs port {SWARM_PORT} free and Redis healthy; "
    f"free the port or set SWARM_PORT in {{env}} to a free port, {AGAIN}",
    "controller": "the controller starts once Redis and the ledger server are healthy and then holds a lease; "
    f"fix those first, {AGAIN}",
}
COLOURS = {"ready": "1;32", "failed": "1;31", "link": "36"}


def parser():
    parser = argparse.ArgumentParser(prog="agentihooks swarm init", description=__doc__)
    parser.add_argument("--timeout", type=int, default=300, help="seconds to wait for every service to be healthy")
    return parser


def stack_home(environ) -> Path:
    return Path(environ.get("AGENTIHOOKS_HOME") or Path.home() / ".agentihooks") / "swarm-stack"


def painter(stream, environ):
    if environ.get("NO_COLOR") or not stream.isatty():
        return lambda text, kind: text
    return lambda text, kind: f"\033[{COLOURS[kind]}m{text}\033[0m"


def docker_problem(run):
    if shutil.which("docker") is None:
        return "Docker is not installed", f"install Docker Engine with the Compose plugin, {AGAIN}"
    for argv, problem, fix in DOCKER_CHECKS:
        if run(argv, capture_output=True, text=True).returncode:
            return problem, fix
    return None


def materialize(home: Path):
    for source in BUNDLED.rglob("*"):
        if source.is_file():
            target = home / source.relative_to(BUNDLED)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    (home / "deploy" / "compose" / "tls").mkdir(parents=True, exist_ok=True)


def free_port(preferred: int) -> int:
    with socket.socket() as probe:
        try:
            probe.bind((BIND, preferred))
        except OSError:
            probe.bind((BIND, 0))
        return probe.getsockname()[1]


def write_private(path: Path, text: str):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as handle:
        handle.write(text)


def read_env(path: Path) -> dict:
    if not path.exists():
        return {}
    lines = (line.partition("=") for line in path.read_text().splitlines() if not line.startswith("#"))
    return {key.strip(): value.strip() for key, sep, value in lines if sep}


def settings(env_file: Path) -> dict:
    values = read_env(env_file)
    defaults = {
        "AGENTIHOOKS_DEPLOYMENT": lambda: "compose",
        "SWARM_BIND": lambda: BIND,
        "SWARM_HIVE_ID": lambda: "compose-controller",
        "SWARM_REDIS_PASSWORD": secrets.token_urlsafe,
        "SWARM_PORT": lambda: str(free_port(8765)),
        "SWARM_REDIS_PORT": lambda: str(free_port(6379)),
    }
    missing = {key: make() for key, make in defaults.items() if not values.get(key)}
    if missing:
        values.update(missing)
        write_private(env_file, "".join(f"{key}={value}\n" for key, value in values.items()))
    return values


def client_env(values: dict) -> str:
    netloc = f"swarm:{values['SWARM_REDIS_PASSWORD']}@{BIND}:{values['SWARM_REDIS_PORT']}"
    redis_url = urllib.parse.urlunsplit(("redis", netloc, "/0", "", ""))
    return (
        "AGENTIHOOKS_DEPLOYMENT=compose\n"
        f"LEDGER_URL=http://{BIND}:{values['SWARM_PORT']}\n"
        f"AGENTIHOOKS_SWARM_REDIS_URL={redis_url}\n"
    )


def compose(home: Path, *args) -> list:
    return [
        "docker",
        "compose",
        "--project-name",
        PROJECT,
        "--project-directory",
        str(home),
        "--file",
        str(home / "compose.yaml"),
        "--env-file",
        str(home / ".env"),
        *args,
    ]


def health(run, home: Path) -> dict:
    text = run(compose(home, "ps", "--all", "--format", "json"), capture_output=True, text=True).stdout.strip()
    rows = json.loads(text) if text.startswith("[") else [json.loads(line) for line in text.splitlines() if line]
    return {row["Service"]: row.get("Health") or row["State"] for row in rows}


def page_problem(base: str, opener) -> str | None:
    for route, mark in (("/healthz", b""), ("/", b"<title>HOME</title>")):
        try:
            with opener(base + route, timeout=5) as response:
                body = response.read()
        except OSError as exc:
            return f"{base}{route} did not answer ({exc})"
        if mark not in body:
            return f"{base}{route} did not serve the ledger home page"
    return None


def failed(out, paint, problem, fix, logs=""):
    print(paint(f"Swarm stack not ready: {problem}.", "failed"), file=out)
    print(f"  Fix: {fix}.", file=out)
    if logs:
        print(f"  Logs: {LOGS} {logs}", file=out)


def ready(out, paint, home: Path, base: str):
    print(paint("Swarm stack ready: redis, swarm and controller are healthy.", "ready"), file=out)
    print("Next steps:", file=out)
    print(f"  1. Open the ledger: {paint(base + '/', 'link')}", file=out)
    print(f"  2. Point this shell at the stack: set -a; . {home / 'client.env'}; set +a", file=out)
    print(
        "  3. Start a swarm only when you want one: agentihooks swarm <id> create --repo <path>, "
        "then agentihooks swarm <id> start",
        file=out,
    )
    print("Running agentihooks swarm init again is safe: it keeps the password, the ports and the data.", file=out)


def start(run, home: Path, timeout: int, out):
    print("Starting the swarm stack; the first run pulls its images.", file=out)
    up = compose(home, "up", "--detach", "--no-build", "--wait", "--wait-timeout", str(timeout))
    result = run(up, capture_output=True, text=True)
    found = health(run, home)
    sick = [(service, found.get(service, "not created")) for service in SERVICES if found.get(service) != "healthy"]
    if sick and result.stderr.strip():
        print("docker compose up said:", file=out)
        for line in result.stderr.strip().splitlines()[-OUTPUT_LINES:]:
            print(f"  {line}", file=out)
    return sick


def main(argv) -> int:
    return init(argv, os.environ, subprocess.run, urllib.request.urlopen, sys.stdout)


def init(argv, environ, run, opener, out) -> int:
    args = parser().parse_args(argv)
    paint = painter(out, environ)
    if problem := docker_problem(run):
        failed(out, paint, *problem)
        return 1
    home = stack_home(environ)
    materialize(home)
    values = settings(home / ".env")
    write_private(home / "client.env", client_env(values))
    if sick := start(run, home, args.timeout, out):
        for service, state in sick:
            failed(out, paint, f"{service} is {state}", FIXES[service].format(env=home / ".env", **values), service)
        return 1
    base = f"http://{BIND}:{values['SWARM_PORT']}"
    if problem := page_problem(base, opener):
        failed(
            out, paint, "the ledger page does not answer", f"{problem}; read the ledger logs with {LOGS} swarm, {AGAIN}"
        )
        return 1
    ready(out, paint, home, base)
    return 0
