"""`agentihooks serena start|stop|restart|status|release <path>` — lifecycle of the Serena router."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from scripts import mcp_daemon

UNIT_NAME = "agentihooks-serena-router.service"
HOST = "127.0.0.1"
REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = REPO_ROOT / "scripts" / "packaging" / "systemd" / f"{UNIT_NAME}.template"
START_WAIT_SECONDS = 90


def port() -> int:
    return int(os.environ.get("AGENTIHOOKS_SERENA_ROUTER_PORT", "8643"))


def base_url() -> str:
    return f"http://{HOST}:{port()}"


def _pidfile() -> Path:
    return mcp_daemon.state_dir() / "serena-router.pid"


def _logfile() -> Path:
    return mcp_daemon.state_dir() / "logs" / "serena-router.log"


def _unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / UNIT_NAME


def render_unit(python: str) -> str:
    replacements = {
        "__PYTHON__": python,
        "__CWD__": str(REPO_ROOT),
        "__PORT__": str(port()),
        "__SERENA__": shutil.which("serena") or "serena",
        "__PATH__": os.environ.get("PATH", ""),
    }
    text = TEMPLATE.read_text()
    for placeholder, value in replacements.items():
        text = text.replace(placeholder, value)
    return text


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=10, check=False)


def _start_systemd(python: str) -> str:
    unit = _unit_path()
    rendered = render_unit(python)
    if not unit.exists() or unit.read_text() != rendered:
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(rendered)
        _systemctl("daemon-reload")
    _systemctl("reset-failed", UNIT_NAME)
    proc = _systemctl("enable", "--now", UNIT_NAME)
    return "" if proc.returncode == 0 else (proc.stderr or proc.stdout).strip()


def _start_pidfile(python: str) -> str:
    _logfile().parent.mkdir(parents=True, exist_ok=True)
    with open(_logfile(), "ab") as log:
        proc = subprocess.Popen(  # noqa: S603
            [python, "-m", "hooks.serena_router", "--host", HOST, "--port", str(port())],
            cwd=REPO_ROOT,
            env={**os.environ, "SERENA_USAGE_REPORTING": "false"},
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    _pidfile().write_text(str(proc.pid))
    return ""


def start(python: str = sys.executable) -> int:
    if mcp_daemon.port_open(HOST, port()):
        print(f"serena router already running at {base_url()}")
        return 0
    error = _start_systemd(python) if mcp_daemon._systemctl_usable() else _start_pidfile(python)
    if error:
        print(f"serena router failed to start: {error}", file=sys.stderr)
        return 1
    deadline = time.monotonic() + START_WAIT_SECONDS
    while time.monotonic() < deadline:
        if mcp_daemon.port_open(HOST, port()):
            print(f"serena router running at {base_url()}")
            return 0
        time.sleep(1)
    print(f"serena router did not open {base_url()} within {START_WAIT_SECONDS}s; see {_logfile()}", file=sys.stderr)
    return 1


def stop() -> int:
    if _unit_path().exists() and mcp_daemon._systemctl_usable():
        _systemctl("stop", UNIT_NAME)
    pidfile = _pidfile()
    if pidfile.exists():
        pid = int(pidfile.read_text().strip() or 0)
        if mcp_daemon.pid_alive(pid):
            os.kill(pid, signal.SIGTERM)
        pidfile.unlink()
    print("serena router stopped")
    return 0


def _request(path: str, payload: dict | None = None) -> dict | None:
    # A closed loopback port can drop instead of refuse on WSL; the short probe keeps callers from waiting out the HTTP timeout.
    if not mcp_daemon.port_open(HOST, port(), timeout=0.5):
        return None
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(f"{base_url()}{path}", data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 - fixed loopback URL
            return json.loads(resp.read())
    except (urllib.error.URLError, OSError, ValueError):
        return None


def status() -> int:
    body = _request("/status.json")
    if body is None:
        print(f"serena router not running ({base_url()})")
        return 1
    print(f"serena router running at {base_url()} with {len(body['backends'])} backend(s)")
    for row in body["backends"]:
        print(f"  {row['root']}  sessions={row['sessions']} calls={row['calls']} idle={row['idle_seconds']}s")
    return 0


def release(path: str) -> int:
    body = _request("/release", {"path": path})
    return 0 if body is not None else 1


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="agentihooks serena")
    parser.add_argument("action", choices=["start", "stop", "restart", "status", "release"])
    parser.add_argument("path", nargs="?", default="")
    args = parser.parse_args(argv)
    if args.action == "start":
        return start()
    if args.action == "stop":
        return stop()
    if args.action == "restart":
        stop()
        return start()
    if args.action == "release":
        return release(args.path)
    return status()
