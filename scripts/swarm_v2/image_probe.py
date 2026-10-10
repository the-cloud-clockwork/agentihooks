"""SV2-IMG-05 in-image smoke: headless herdr server capabilities and offline harness SessionStart hook delivery."""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from scripts.swarm_v2.worker_home import Request, bootstrap

MANIFEST = Path("/opt/swarm-node/manifest.json")
INTERPRETER = Path("/opt/venv/bin/python")
ATTEMPT = "image-probe"
PROFILES = {"claude": "fixture-claude", "codex": "fixture-codex"}
ACCOUNTS = {"claude": "AH_CC_TOKEN_FIXTURE", "codex": "AH_CX_TOKEN_FIXTURE"}
ENDPOINTS = {"AGENTIHOOKS_LEDGER_URL": "http://ledger.swarm.invalid:8765"}
LAUNCHES = {
    "claude": (["claude", "-p", "hello"], 60),
    "codex": (["codex", "exec", "--skip-git-repo-check", "--dangerously-bypass-hook-trust", "hello"], 20),
}
STARTUP_SECONDS = 20.0
POLL_SECONDS = 0.2


def run(command: list[str], environ: dict, timeout: float, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        command, env=environ, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout
    )


def version(name: str, environ: dict) -> str:
    return run([name, "--version"], environ, 30).stdout.strip()


def methods(schema: dict) -> list[str]:
    requests = schema.get("schemas", {}).get("request", {}).get("oneOf", [])
    return sorted(request["properties"]["method"]["const"] for request in requests)


def server_status(environ: dict, clock=time.monotonic, sleep=time.sleep) -> dict:
    deadline = clock() + STARTUP_SECONDS
    while True:
        try:
            status = json.loads(run(["herdr", "status", "server", "--json"], environ, 10).stdout)
        except ValueError:
            status = {}
        if status.get("running") or clock() >= deadline:
            return status
        sleep(POLL_SECONDS)


def herdr(root: Path, environ: dict) -> dict:
    (root / "tmp").mkdir(mode=0o700, parents=True, exist_ok=True)
    environ = environ | {
        "HOME": str(root),
        "XDG_RUNTIME_DIR": str(root / "tmp"),
        "HERDR_CONFIG_PATH": str(root / "herdr.toml"),
    }
    devnull = subprocess.DEVNULL
    server = subprocess.Popen(["herdr", "server"], env=environ, stdin=devnull, stdout=devnull, stderr=devnull)
    try:
        status = server_status(environ)
        schema = json.loads(run(["herdr", "api", "schema", "--json"], environ, 30).stdout or "{}")
    finally:
        server.terminate()
        server.wait(timeout=10)
    found = {"protocol": schema.get("protocol"), "methods": methods(schema)}
    return {"version": version("herdr", environ), "status": status, "schema": found}


def registrations(home: Path) -> int:
    try:
        return len(json.loads((home / ".agentihooks" / "active-sessions.json").read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return 0


def harness(name: str, attempt: Path, environ: dict) -> dict:
    home, work = attempt / "homes" / name, attempt / "work"
    run(["git", "init", "-q", str(work)], environ, 30)
    environ = environ | {"HOME": str(home)}
    command, timeout = LAUNCHES[name]
    try:
        run(command, environ, timeout, cwd=work)
    except subprocess.TimeoutExpired:
        pass
    return {"version": version(name, environ), "hook_registrations": registrations(home)}


def probe(templates: Path, root: Path, environ: dict) -> dict:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    request = Request(root, ATTEMPT, templates, PROFILES, INTERPRETER, ACCOUNTS, ENDPOINTS, os.getuid(), os.getgid())
    bootstrap(request)
    base = {key: value for key, value in environ.items() if not key.startswith("HERDR_")}
    base["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    observed = {"herdr": herdr(root / "herdr", base)}
    observed |= {name: harness(name, root / ATTEMPT, base) for name in PROFILES}
    return {"manifest": json.loads(MANIFEST.read_text(encoding="utf-8")), "observed": observed}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.swarm_v2.image_probe")
    parser.add_argument("--templates", type=Path, default=Path("/opt/probe/profiles"))
    parser.add_argument("--root", type=Path, default=Path("/home/worker/attempts"))
    args = parser.parse_args(argv)
    print(json.dumps(probe(args.templates, args.root, dict(os.environ)), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
