import json
import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path

import jsonschema

from scripts.headless import runner, tiers

WORKER_RUN = "AGENTIHOOKS_WORKER_RUN"
WORKER_RUN_ID = "AGENTIHOOKS_WORKER_RUN_ID"
TRANSCRIPT = "transcript.jsonl"
STDERR = "stderr.txt"
CODEX_LAST_MESSAGE = "last-message.txt"
CODEX_OUTPUT_SCHEMA = "output-schema.json"


@dataclass(frozen=True)
class ProfileRun:
    profile: str
    harness: str
    input: str
    schema: dict | None
    tier: str
    effort: str
    timeout: float
    run_dir: Path


@dataclass(frozen=True)
class ProfileResult:
    status: str
    output: object
    transcript: Path
    session: str | None
    exit_code: int | None


def claude_args(run: ProfileRun) -> list[str]:
    args = [
        "-p",
        run.input,
        "--output-format",
        "stream-json",
        "--verbose",
        "--no-session-persistence",
        "--permission-mode",
        "bypassPermissions",
    ]
    if run.schema is not None:
        args += ["--json-schema", json.dumps(run.schema)]
    return args


def codex_args(run: ProfileRun) -> list[str]:
    args = ["exec", "--json", "-o", str(run.run_dir / CODEX_LAST_MESSAGE)]
    if run.schema is not None:
        args += ["--output-schema", str(run.run_dir / CODEX_OUTPUT_SCHEMA)]
    return [*args, "--", run.input]


HARNESS_ARGS = {"claude": claude_args, "codex": codex_args}


def claude_result(text: str) -> tuple[object, str | None]:
    for line in reversed(text.splitlines()):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            structured = event.get("structured_output")
            output = event.get("result", "") if structured is None else structured
            return output, event.get("session_id")
    return "", None


def codex_result(run: ProfileRun, text: str) -> tuple[object, str | None]:
    session = None
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "thread.started":
            session = event.get("thread_id")
            break
    message = run.run_dir / CODEX_LAST_MESSAGE
    return (message.read_text() if message.exists() else ""), session


def execute(argv: list[str], env: dict[str, str], run_dir: Path, timeout: float) -> int | None:
    with (run_dir / TRANSCRIPT).open("w") as out, (run_dir / STDERR).open("w") as err:
        process = subprocess.Popen(
            argv, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=err, start_new_session=True
        )
        try:
            return process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            # select-profile spawns the harness as a grandchild, so kill the whole process group
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            process.wait()
            return None


def run_profile(run: ProfileRun) -> ProfileResult:
    harness = runner.harness_for(run.harness, os.environ)
    run.run_dir.mkdir(parents=True, exist_ok=True)
    model = tiers.model(harness, run.tier, os.environ)
    if run.schema is not None and harness == "codex":
        (run.run_dir / CODEX_OUTPUT_SCHEMA).write_text(json.dumps(run.schema))
    argv = [
        "agentihooks",
        "select-profile",
        run.profile,
        "--agent",
        harness,
        "--model",
        model,
        "--effort",
        run.effort,
        "--",
        *HARNESS_ARGS[harness](run),
    ]
    env = {**os.environ, WORKER_RUN: "1", WORKER_RUN_ID: run.run_dir.name}
    transcript = run.run_dir / TRANSCRIPT
    exit_code = execute(argv, env, run.run_dir, run.timeout)
    if exit_code is None:
        return ProfileResult("timeout", None, transcript, None, None)
    text = transcript.read_text()
    output, session = claude_result(text) if harness == "claude" else codex_result(run, text)
    if exit_code != 0:
        return ProfileResult("failed", output, transcript, session, exit_code)
    if run.schema is None:
        return ProfileResult("ok", output, transcript, session, exit_code)
    try:
        validated = runner.validated(output, run.schema)
    except (ValueError, jsonschema.ValidationError):
        return ProfileResult("invalid", output, transcript, session, exit_code)
    return ProfileResult("ok", validated, transcript, session, exit_code)
