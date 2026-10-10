import argparse
import http.client
import itertools
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from hooks.proc import _process
from scripts.swarm_v2 import filesystem
from scripts.swarm_v2.supervision_protocol import matches, read

MODES = ("startup", "liveness", "readiness", "diagnose")
HARNESSES = ("claude", "codex")
HERDR_TIMEOUT = 2.0
BRAIN_TIMEOUT = 2.0
VARIABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")
SHELL_OPERATORS = frozenset(("&&", "||", ";", "|"))


@dataclass(frozen=True)
class Probe:
    attempt: Path
    harness: str
    environ: dict
    herdr_timeout: float = HERDR_TIMEOUT
    brain_timeout: float = BRAIN_TIMEOUT


def private_home(execution: filesystem.Execution, harness: str) -> Path | None:
    homes = read(execution.root / "execution.json").get("homes")
    relative = homes.get(harness) if isinstance(homes, dict) else None
    if not isinstance(relative, str):
        return None
    home = (execution.root / relative).resolve()
    return home if home.is_relative_to(execution.path("home").resolve()) and home.is_dir() else None


def writable(path: Path) -> bool:
    try:
        with tempfile.TemporaryFile(dir=path):
            return True
    except OSError:
        return False


def home_failure(home: Path | None) -> str | None:
    if home is None:
        return "home_missing"
    if not os.access(home, os.R_OK | os.X_OK):
        return "home_unreadable"
    return None if writable(home) else "home_unwritable"


def runtime_failure(execution: filesystem.Execution) -> str | None:
    for path in (execution.path("runtime"), execution.path("scratch")):
        if not writable(path):
            return f"runtime_path_unwritable:{path.name}"
    return None


def binary_failure(harness: str, environ: dict) -> str | None:
    for name in ("herdr", harness):
        if shutil.which(name, path=environ.get("PATH", "")) is None:
            return f"missing_binary:{name}"
    return None


def session_start_commands(home: Path, harness: str) -> list:
    path = home / ".codex" / "hooks.json" if harness == "codex" else home / ".claude" / "settings.json"
    hooks = read(path).get("hooks")
    groups = hooks.get("SessionStart") if isinstance(hooks, dict) else None
    handlers = [h for g in groups or [] if isinstance(g, dict) for h in g.get("hooks") or [] if isinstance(h, dict)]
    return [h["command"] for h in handlers if isinstance(h.get("command"), str)]


def command_segments(command: str) -> list[list[str]]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    segments = [[]]
    for token in lexer:
        if token in SHELL_OPERATORS:
            segments.append([])
        else:
            segments[-1].append(token)
    return segments


def segments_callable(segments: list[list[str]], path: str) -> bool:
    directory = Path.cwd()
    for words in segments:
        words = list(itertools.dropwhile(ASSIGNMENT.fullmatch, words))
        if not words:
            return False
        if words[0] == "cd":
            if len(words) != 2 or not (directory / Path(words[1]).expanduser()).is_dir():
                return False
            directory = directory / Path(words[1]).expanduser()
            continue
        program = str(directory / Path(words[0]).expanduser()) if "/" in words[0] else words[0]
        if shutil.which(program, path=path) is None:
            return False
    return True


def hook_failure(home: Path | None, harness: str, environ: dict) -> str | None:
    commands = session_start_commands(home, harness) if home else []
    if not commands:
        return "hook_missing"
    for command in commands:
        try:
            segments = command_segments(command)
        except ValueError:
            return "hook_invalid"
        if segments == [[]]:
            return "hook_invalid"
        if not segments_callable(segments, environ.get("PATH", "")):
            return "hook_uncallable"
    return None


def alive(scope: dict) -> bool:
    pid = scope.get("supervisor_pid")
    if type(pid) is not int:
        return False
    found = _process(pid, Path("/proc"))
    if found is None or found.state == "Z":
        return False
    try:
        return str(Path(f"/proc/{pid}/ns/pid").readlink()) == scope.get("process_namespace")
    except OSError:
        return False


def incarnation(execution: filesystem.Execution) -> tuple[Path, dict] | None:
    contexts = sorted(
        (execution.path("runtime") / "supervision").glob("*/context.json"), key=lambda p: p.stat().st_mtime_ns
    )
    if not contexts:
        return None
    scope = read(contexts[-1])
    return (contexts[-1].parent, scope) if alive(scope) and not (contexts[-1].parent / "result.json").exists() else None


def herdr_failure(probe: Probe, execution: filesystem.Execution, home: Path | None, root: Path | None) -> str | None:
    if home is None or root is None:
        return "herdr_unavailable"
    environment = {k: v for k, v in probe.environ.items() if not k.startswith("HERDR_")}
    environment.update(filesystem.environment(execution, probe.harness), HERDR_CONFIG_PATH=str(root / "herdr.toml"))
    try:
        result = subprocess.run(
            ["herdr", "workspace", "list"],
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=probe.herdr_timeout,
            check=True,
        )
        answer = json.loads(result.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return "herdr_unavailable"
    return None if isinstance(answer, dict) else "herdr_unavailable"


def agent_failure(root: Path | None, scope: dict) -> str | None:
    running = read(root / "running.json") if root else {}
    return None if matches(running, scope) and running.get("status") == "running" else "agent_not_running"


def brain_state(environ: dict, timeout: float) -> str:
    url = environ.get("BRAIN_URL", "").strip().rstrip("/")
    if not url:
        return "unconfigured"
    token = environ.get("BRAIN_HTTP_TOKEN") or environ.get("KB_ROUTER_TOKEN")
    try:
        request = urllib.request.Request(f"{url}/health", headers={"Authorization": f"Bearer {token}"} if token else {})
        with urllib.request.urlopen(request, timeout=timeout):
            return "ok"
    except urllib.error.HTTPError as exc:
        return f"brain_http_{exc.code}"
    except (OSError, ValueError, http.client.HTTPException):
        return "brain_unreachable"


def local_checks(probe: Probe, execution: filesystem.Execution, mode: str, found: tuple[Path, dict] | None) -> dict:
    root, scope = found or (None, {})
    if mode == "liveness":
        return {"supervisor": None if found else "supervisor_absent"}
    home = private_home(execution, probe.harness)
    checks = {
        "binaries": binary_failure(probe.harness, probe.environ),
        "home": home_failure(home),
        "runtime_paths": runtime_failure(execution),
        "hook": hook_failure(home, probe.harness, probe.environ),
        "supervisor": None if found else "supervisor_absent",
        "herdr": herdr_failure(probe, execution, home, root),
    }
    if mode != "startup":
        checks["agent"] = agent_failure(root, scope)
    return checks


def evaluate(probe: Probe, mode: str) -> dict:
    execution = filesystem.recorded(probe.attempt.resolve(), read(probe.attempt / "execution.json"))
    found = incarnation(execution)
    checks = local_checks(probe, execution, mode, found)
    reason = next((value for value in checks.values() if value), None)
    report = {
        "mode": mode,
        "harness": probe.harness,
        "attempt": str(probe.attempt),
        "incarnation": found[0].name if found else None,
        "checks": checks,
        "worker_startup_failure_reason": reason,
    }
    if mode == "liveness":
        return {**report, "status": "not_live" if reason else "live"}
    dependencies = {"brain": brain_state(probe.environ, probe.brain_timeout)}
    degraded = any(state not in ("ok", "unconfigured") for state in dependencies.values())
    report.update(dependencies=dependencies, status="not_ready" if reason else "degraded" if degraded else "ready")
    if mode == "diagnose":
        report["environment"] = sorted(name for name in probe.environ if VARIABLE.fullmatch(name))
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="health.py")
    parser.add_argument("mode", choices=MODES)
    parser.add_argument("--attempt", required=True, type=Path)
    parser.add_argument("--harness", required=True, choices=HARNESSES)
    parser.add_argument("--herdr-timeout", type=float, default=HERDR_TIMEOUT)
    parser.add_argument("--brain-timeout", type=float, default=BRAIN_TIMEOUT)
    return parser


def main(argv: list[str] | None = None, environ: dict | None = None) -> int:
    try:
        args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    except SystemExit as exc:
        return 0 if exc.code == 0 else 64
    environ = dict(os.environ) if environ is None else environ
    probe = Probe(args.attempt, args.harness, environ, args.herdr_timeout, args.brain_timeout)
    report = evaluate(probe, args.mode)
    print(json.dumps(report, sort_keys=True))
    return 0 if args.mode == "diagnose" or report["status"] in ("live", "ready", "degraded") else 1


if __name__ == "__main__":
    raise SystemExit(main())
