import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from scripts.swarm_v2.worker_home import Request, bootstrap

MANIFEST = Path("/opt/swarm-node/manifest.json")
TEMPLATES = Path("/opt/probe/profiles")
ROOT = Path("/home/worker/attempts")
INTERPRETER = Path("/opt/venv/bin/python")
ATTEMPT = "image-probe"
PROFILES = {"claude": "fixture-claude", "codex": "fixture-codex"}
ACCOUNTS = {"claude": "AH_CC_TOKEN_FIXTURE", "codex": "AH_CX_TOKEN_FIXTURE"}
ENDPOINTS = {"AGENTIHOOKS_LEDGER_URL": "http://ledger.swarm.invalid:8765"}
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


def herdr(root: Path, environ: dict) -> dict:
    (root / "tmp").mkdir(mode=0o700, parents=True)
    environ = environ | {
        "HOME": str(root),
        "XDG_RUNTIME_DIR": str(root / "tmp"),
        "HERDR_CONFIG_PATH": str(root / "herdr.toml"),
    }
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


def harness(name: str, attempt: Path, environ: dict) -> dict:
    home, work = attempt / "homes" / name, attempt / "work"
    run(["git", "init", "-q", str(work)], environ, COMMAND_SECONDS)
    environ = environ | {"HOME": str(home)}
    command, timeout = LAUNCHES[name]
    try:
        run(command, environ, timeout, cwd=work)
    except subprocess.TimeoutExpired:
        pass
    return {"version": version(name, environ), "hook_registrations": registrations(home)}


def probe(templates: Path, root: Path, environ: dict) -> dict:
    root.mkdir(mode=0o700, exist_ok=True)
    bootstrap(Request(root, ATTEMPT, templates, PROFILES, INTERPRETER, ACCOUNTS, ENDPOINTS, os.getuid(), os.getgid()))
    base = {key: value for key, value in environ.items() if not key.startswith("HERDR_")} | OFFLINE
    observed = {"herdr": herdr(root / "herdr", base)}
    observed |= {name: harness(name, root / ATTEMPT, base) for name in PROFILES}
    return {"manifest": json.loads(MANIFEST.read_bytes()), "observed": observed}


def main() -> int:
    print(json.dumps(probe(TEMPLATES, ROOT, dict(os.environ)), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
