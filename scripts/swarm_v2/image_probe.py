import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from scripts.swarm_v2 import filesystem
from scripts.swarm_v2.worker_home import Request, bootstrap

MANIFEST = Path("/opt/swarm-node/manifest.json")
TEMPLATES = Path("/opt/probe/profiles")
ROOT = Path("/home/worker/attempts")
INTERPRETER = Path("/opt/venv/bin/python")
ATTEMPT = "image-probe"
PROFILES = {"claude": "fixture-claude", "codex": "fixture-codex"}
ACCOUNTS = {"claude": "AH_CC_TOKEN_FIXTURE", "codex": "AH_CX_TOKEN_FIXTURE"}
ENDPOINTS = {"AGENTIHOOKS_LEDGER_URL": "https://ledger.swarm.invalid:8765"}
OFFLINE = {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
LAUNCHES = {
    "claude": (["claude", "-p", "hello"], 60),
    "codex": (["codex", "exec", "--skip-git-repo-check", "--dangerously-bypass-hook-trust", "hello"], 20),
}
HERDR_SERVER = ["herdr", "server"]
HERDR_STATUS = ["herdr", "status", "server", "--json"]
HERDR_SCHEMA = ["herdr", "api", "schema", "--json"]
COMMAND_SECONDS = 30
STATUS_SECONDS = 10
STOP_SECONDS = 10
STARTUP_SECONDS = 20.0
POLL_SECONDS = 0.2


def run(command: list[str], environ: dict, timeout: float, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        command, env=environ, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout
    )


def version(name: str, environ: dict) -> str:
    return run([name, "--version"], environ, COMMAND_SECONDS).stdout.strip()


def methods(schema: dict) -> list[str]:
    requests = schema.get("schemas", {}).get("request", {}).get("oneOf", [])
    found = (request.get("properties", {}).get("method", {}).get("const") for request in requests)
    return sorted(name for name in found if isinstance(name, str))


def parsed(text: str) -> dict:
    try:
        document = json.loads(text)
    except ValueError:
        return {}
    return document if isinstance(document, dict) else {}


def server_status(
    environ: dict, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep
) -> dict:
    deadline = clock() + STARTUP_SECONDS
    while True:
        try:
            status = json.loads(run(HERDR_STATUS, environ, STATUS_SECONDS).stdout)
        except (ValueError, subprocess.TimeoutExpired):
            status = {}
        if status.get("running") or clock() >= deadline:
            return status
        sleep(POLL_SECONDS)


def herdr(execution: filesystem.Execution, environ: dict) -> dict:
    environ = (
        environ | filesystem.environment(execution, "herdr") | {"HERDR_CONFIG_PATH": str(execution.root / "herdr.toml")}
    )
    Path(environ["HOME"]).mkdir(mode=0o700)
    devnull = subprocess.DEVNULL
    server = subprocess.Popen(HERDR_SERVER, env=environ, stdin=devnull, stdout=devnull, stderr=devnull)
    try:
        status = server_status(environ)
        schema = parsed(run(HERDR_SCHEMA, environ, COMMAND_SECONDS).stdout)
    finally:
        server.terminate()
        server.wait(timeout=STOP_SECONDS)
    found = {"protocol": schema.get("protocol"), "methods": methods(schema)}
    return {"version": version(HERDR_SERVER[0], environ), "status": status, "schema": found}


def registrations(home: Path) -> int:
    try:
        return len(json.loads((home / ".agentihooks" / "active-sessions.json").read_bytes()))
    except (OSError, ValueError):
        return 0


def harness(name: str, execution: filesystem.Execution, environ: dict) -> dict:
    home, work = execution.path("home") / name, execution.root / "work"
    run(["git", "init", "-q", str(work)], environ, COMMAND_SECONDS)
    environ = environ | filesystem.environment(execution, name)
    command, timeout = LAUNCHES[name]
    try:
        run(command, environ, timeout, cwd=work)
    except subprocess.TimeoutExpired:
        pass
    return {"version": version(name, environ), "hook_registrations": registrations(home)}


def probe(templates: Path, root: Path, environ: dict) -> dict:
    root.mkdir(mode=0o700, exist_ok=True)
    record = bootstrap(
        Request(root, ATTEMPT, templates, PROFILES, INTERPRETER, ACCOUNTS, ENDPOINTS, os.getuid(), os.getgid())
    )
    execution = filesystem.recorded(root / ATTEMPT, record)
    base = {key: value for key, value in environ.items() if not key.startswith("HERDR_")} | OFFLINE
    observed = {"herdr": herdr(filesystem.allocate(root, "herdr", execution.layout), base)}
    observed |= {name: harness(name, execution, base) for name in PROFILES}
    return {"manifest": json.loads(MANIFEST.read_bytes()), "observed": observed}


def main() -> int:
    print(json.dumps(probe(TEMPLATES, ROOT, dict(os.environ)), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
