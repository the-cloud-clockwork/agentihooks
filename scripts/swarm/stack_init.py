"""The stack step of agentihooks swarm init: bring up the local swarm stack with Docker Compose and print next steps.

It never creates or starts a swarm; swarm init creates it after this step and the operator starts it.
"""

import argparse
import ipaddress
import json
import os
import shlex
import shutil
import subprocess
import urllib.parse
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TextIO

from scripts.swarm.stack_env import BIND, client_env, read_env, settings, write_private

PROJECT = "agentihooks-swarm"
BUNDLED = Path(__file__).with_name("stack")
SERVICES = ("redis", "nats", "swarm", "controller")
ADOPTION = "deploy/compose/adopt.yaml"
PREPARED_CLIENT = "swarm-client.env"
LEDGER_PORT = 8765
AGAIN = "then run agentihooks swarm init again"
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
    "redis": "read its logs; if port {SWARM_REDIS_PORT} is in use, set SWARM_REDIS_PORT in {env} to a free port, "
    "and keep the password in {env} as it was at the first start, {again}",
    "nats": "read its logs; NATS keeps its JetStream store on the nats-data volume, so free space there "
    "or remove that volume to start the bus empty, {again}",
    "swarm": "read its logs; the ledger server needs Redis healthy, and if port {SWARM_PORT} is in use, "
    "set SWARM_PORT in {env} to a free port, {again}",
    "adopted": "read its logs; the adopted ledger server serves port {SWARM_PORT} on the host network, "
    "so stop the legacy ledger container by its id first, {again}",
    "controller": "read its logs; the controller starts once Redis and the ledger server are healthy "
    "and then holds a lease, so fix those first, {again}",
}
COLOURS = {"ready": "1;32", "failed": "1;31", "link": "36"}

Run = Callable[..., subprocess.CompletedProcess]
Paint = Callable[[str, str], str]


def parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentihooks swarm init", description=__doc__)
    parser.add_argument("--timeout", type=int, default=300, help="seconds to wait for every service to be healthy")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--prepare-clients",
        action="store_true",
        help="verify bundle credentials on existing local Redis and activate clients",
    )
    mode.add_argument(
        "--adopt",
        action="store_true",
        help="serve the existing shared ledger folder as this user from the stack, on the default ledger port, "
        "and keep doing so on every later run",
    )
    parser.add_argument(
        "--no-start",
        action="store_true",
        help="write the stack files and settings, adoption included, without starting any service",
    )
    return parser


def stack_home(environ: Mapping[str, str]) -> Path:
    return Path(environ.get("AGENTIHOOKS_HOME") or Path.home() / ".agentihooks") / "swarm-stack"


def painter(stream: TextIO, environ: Mapping[str, str]) -> Paint:
    if environ.get("NO_COLOR") or not stream.isatty():
        return lambda text, kind: text
    return lambda text, kind: f"\033[{COLOURS[kind]}m{text}\033[0m"


def docker_problem(run: Run) -> tuple[str, str] | None:
    if shutil.which("docker") is None:
        return "Docker is not installed", f"install Docker Engine with the Compose plugin, {AGAIN}"
    for argv, problem, fix in DOCKER_CHECKS:
        if run(argv, capture_output=True, text=True).returncode:
            return problem, fix
    return None


def materialize(home: Path) -> None:
    for source in BUNDLED.rglob("*"):
        if source.is_file():
            target = home / source.relative_to(BUNDLED)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    (home / "deploy" / "compose" / "tls").mkdir(exist_ok=True)


def loopback(url: str) -> bool:
    try:
        host = urllib.parse.urlsplit(url).hostname
        return host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def adopt(env_file: Path, environ: Mapping[str, str]) -> bool:
    from scripts.swarm_ledger.server_compose import ROOT

    client = read_env(env_file.parent.parent / PREPARED_CLIENT).get("AGENTIHOOKS_SWARM_REDIS_URL")
    if not client:
        return False
    values = read_env(env_file)
    values.update(
        {
            "SWARM_ADOPT_LEDGER_DIR": str(
                Path(environ.get("LEDGER_DIR") or Path.home() / "development-ledger").expanduser()
            ),
            "SWARM_ADOPT_HOME": str(Path.home()),
            "SWARM_ADOPT_UID": str(os.getuid()),
            "SWARM_ADOPT_GID": str(os.getgid()),
            "SWARM_ADOPT_CODE": str(ROOT),
            "SWARM_ADOPT_REDIS_URL": shlex.split(client)[0],
            "SWARM_PORT": str(LEDGER_PORT),
        }
    )
    values.update(
        {
            key: environ[key]
            for key in ("AGENTIHOOKS_SWARM_DATABASE_URL", "AGENTIHOOKS_SWARM_REDIS_URL")
            if environ.get(key)
        }
    )
    if "AGENTIHOOKS_SWARM_REDIS_URL" in values and loopback(values["AGENTIHOOKS_SWARM_REDIS_URL"]):
        del values["AGENTIHOOKS_SWARM_REDIS_URL"]
    write_private(env_file, "".join(f"{key}={value}\n" for key, value in values.items()))
    return True


def project(home: Path) -> str:
    return read_env(home / ".env").get("SWARM_COMPOSE_PROJECT") or PROJECT


def logs(home: Path) -> str:
    return f"docker compose --project-name {project(home)} logs"


def compose(home: Path, *args: str) -> list[str]:
    adoption = ["--file", str(home / ADOPTION)] if read_env(home / ".env").get("SWARM_ADOPT_LEDGER_DIR") else []
    return [
        "docker",
        "compose",
        "--project-name",
        project(home),
        "--project-directory",
        str(home),
        "--file",
        str(home / "compose.yaml"),
        *adoption,
        "--env-file",
        str(home / ".env"),
        *args,
    ]


def health(run: Run, home: Path) -> tuple[dict[str, str], str]:
    listing = run(compose(home, "ps", "--all", "--format", "json"), capture_output=True, text=True)
    if listing.returncode:
        return {}, listing.stderr
    text = listing.stdout.strip()
    rows = json.loads(text) if text.startswith("[") else [json.loads(line) for line in text.splitlines() if line]
    return {row["Service"]: row.get("Health") or row["State"] for row in rows}, ""


def page_problem(base: str, opener: Callable) -> str | None:
    for route, mark in (("/healthz", b""), ("/", b"<title>HOME</title>")):
        try:
            with opener(base + route, timeout=5) as response:
                body = response.read()
        except OSError as exc:
            return f"{base}{route} did not answer ({exc})"
        if mark not in body:
            return f"{base}{route} did not serve the ledger home page"
    return None


def failed(out: TextIO, paint: Paint, problem: str, fix: str, command: str = "") -> None:
    print(paint(f"Swarm stack not ready: {problem}.", "failed"), file=out)
    print(f"  Fix: {fix}.", file=out)
    if command:
        print(f"  Logs: {command}", file=out)


def ready(out: TextIO, paint: Paint, home: Path, base: str, name: str) -> None:
    print(paint("Swarm stack ready: redis, nats, swarm and controller are healthy.", "ready"), file=out)
    print("Next steps:", file=out)
    print(f"  1. Open the ledger: {paint(base + '/', 'link')}", file=out)
    print(f"  2. Point this shell at the stack: set -a; . {home / 'client.env'}; set +a", file=out)
    print(f"  3. Start the swarm only when you want it: agentihooks swarm {name} start", file=out)
    print("Running agentihooks swarm init again is safe: it keeps the password, the ports and the data.", file=out)


def start(run: Run, home: Path, timeout: int, out: TextIO) -> list[tuple[str, str]]:
    print("Starting the swarm stack; the first run pulls its images.", file=out)
    up = compose(home, "up", "--detach", "--no-build", "--wait", "--wait-timeout", str(timeout))
    result = run(up, capture_output=True, text=True)
    found, failure = health(run, home)
    absent = "in an unknown state" if failure else "not created"
    sick = [(service, found.get(service, absent)) for service in SERVICES if found.get(service) != "healthy"]
    said = [line for part in (result.stderr, failure) for line in part.strip().splitlines()]
    if sick and said:
        print("docker compose said:", file=out)
        for line in said[-OUTPUT_LINES:]:
            print(f"  {line}", file=out)
    return sick


def init(argv: list[str], environ: Mapping[str, str], run: Run, opener: Callable, out: TextIO, name: str) -> int:
    args = parser().parse_args(argv)
    if args.prepare_clients:
        from scripts.swarm.stack_clients import prepare

        return prepare(stack_home(environ), environ, out)
    paint = painter(out, environ)
    if problem := docker_problem(run):
        failed(out, paint, *problem)
        return 1
    home = stack_home(environ)
    materialize(home)
    if args.adopt and not adopt(home / ".env", environ):
        fix = "run agentihooks swarm init --prepare-clients, then agentihooks swarm init --adopt"
        failed(out, paint, "no prepared swarm Redis client to adopt", fix)
        return 1
    values = settings(home / ".env", environ)
    write_private(home / "client.env", client_env(values, home))
    if args.no_start:
        print(f"Swarm stack settings written to {home}; no service was started.", file=out)
        return 0
    if sick := start(run, home, args.timeout, out):
        for service, state in sick:
            adopted = service == "swarm" and values.get("SWARM_ADOPT_LEDGER_DIR")
            fix = FIXES["adopted" if adopted else service].format(env=home / ".env", again=AGAIN, **values)
            failed(out, paint, f"{service} is {state}", fix, f"{logs(home)} {service}")
        return 1
    base = urllib.parse.urlunsplit(("http", f"{BIND}:{values['SWARM_PORT']}", "", "", ""))
    if problem := page_problem(base, opener):
        fix = f"{problem}; read the ledger logs with {logs(home)} swarm, {AGAIN}"
        failed(out, paint, "the ledger page does not answer", fix)
        return 1
    ready(out, paint, home, base, name)
    return 0
