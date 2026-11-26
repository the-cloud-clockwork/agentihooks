from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable, Mapping

from hooks import common
from hooks.context.account_sessions import CODEX_DEFAULT, UNROUTED, codex_account_from_names, environment_account
from scripts.herdr_setup import resume_off

SOURCE = "agentihooks"
TIMEOUT_S = 3.0
FAILED = "herdr resume report failed"
HOMES = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME"}


log: Callable[[str, dict[str, str]], None] = getattr(common, "log")


def routed(environ: Mapping[str, str], target: str) -> bool:
    if target == "codex":
        return codex_account_from_names(name for name, value in environ.items() if value) != CODEX_DEFAULT
    return environment_account(environ) != UNROUTED


def resume_argv(environ: Mapping[str, str], session_id: str, target: str) -> list[str] | None:
    if not session_id or environ.get("HERDR_ENV") != "1" or not environ.get("HERDR_PANE_ID"):
        return None
    if not environ.get("HERDR_BIN_PATH") or environ.get("AGENTIHOOKS_SWARM") or resume_off(environ):
        return None
    if target not in HOMES or not routed(environ, target):
        return None
    home = environ.get(HOMES[target])
    prefix = ["env", f"{HOMES[target]}={home}"] if home else []
    if target == "codex":
        argv = [*prefix, "agentihooks", "codex", "resume", session_id]
    else:
        argv = [*prefix, "agentihooks", "claude", "--agentihooks-fallback-bare", "--resume", session_id]
    return None if any("'" in arg for arg in argv) else argv


def report(
    payload: Mapping[str, object],
    environ: Mapping[str, str] | None = None,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> bool:
    active = os.environ if environ is None else environ
    command = report_command(payload, active)
    if command is None:
        return False
    try:
        done = run(command, capture_output=True, text=True, timeout=TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(FAILED, {"error": type(exc).__name__})
        return False
    if done.returncode != 0:
        log(FAILED, {"error": (done.stdout or done.stderr).strip()[:200]})
        return False
    return True


def report_command(payload: Mapping[str, object], environ: Mapping[str, str]) -> list[str] | None:
    target = environ.get("AGENTIHOOKS_TARGET", "").strip().lower() or "claude"
    session_id = payload.get("session_id")
    argv = resume_argv(environ, session_id, target) if isinstance(session_id, str) else None
    if argv is None:
        return None
    seq = str(time.time_ns())
    pane = environ["HERDR_PANE_ID"]
    flags = ["--source", SOURCE, "--agent", target, "--seq", seq]
    return [environ["HERDR_BIN_PATH"], "pane", "report-agent-session", pane, *flags, "--", *argv]


def kick(payload: Mapping[str, object], environ: Mapping[str, str] | None = None) -> bool:
    active = os.environ if environ is None else environ
    if report_command(payload, active) is None:
        return False
    from hooks._async import fork_and_call

    fork_and_call(report, dict(payload), dict(active), timeout_sec=10, task_name="herdr-resume-report")
    return True
