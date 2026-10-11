import json
import os
import signal
import subprocess
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

MIN_MS = 60_000
MAX_MS = 600_000
TIMEOUT_S = 30
PROMPT = "Reply with exactly OK."


@dataclass(frozen=True)
class Target:
    harness: str
    account: str
    model: str


@dataclass(frozen=True)
class Result:
    ok: bool
    evidence: str
    observed_at: int | None = None


def _result(ok: bool, evidence: str) -> Result:
    return Result(ok, evidence, time.time_ns() // 1_000_000)


def key(target: Target) -> str:
    return f"{target.harness}:{target.account}"


def schedule(target: Target, now_ms: int) -> dict:
    return {"target": asdict(target), "next_at": now_ms + MIN_MS, "delay_ms": MIN_MS, "succeeded_at": None}


def advance(states: dict, now_ms: int, probe: Callable[[Target], Result]) -> bool:
    due = [state for state in states.values() if state["succeeded_at"] is None and state["next_at"] <= now_ms]
    if due:
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(probe, [Target(**state["target"]) for state in due]))
        for state, result in zip(due, results):
            at = result.observed_at if result.observed_at is not None else now_ms
            state["evidence"] = result.evidence
            if result.ok:
                state["succeeded_at"] = at
            else:
                state["delay_ms"] = min(state["delay_ms"] * 2, MAX_MS)
                state["next_at"] = at + state["delay_ms"]
    return bool(states) and all(state["succeeded_at"] is not None for state in states.values())


def _arguments(target: Target) -> list[str]:
    model = ["--model", target.model] if target.model else []
    if target.harness == "claude":
        return [
            "agentihooks",
            "claude",
            "--route",
            target.account,
            "-p",
            *model,
            "--output-format",
            "json",
            "--tools",
            "",
            "--no-session-persistence",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--settings",
            '{"disableAllHooks":true}',
            "--system-prompt",
            PROMPT,
        ]
    if target.harness == "codex":
        return [
            "agentihooks",
            "codex",
            "--route",
            target.account,
            "exec",
            *model,
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "--ephemeral",
            "--json",
            "-c",
            'model_reasoning_effort="low"',
            "-c",
            f"developer_instructions={json.dumps(PROMPT)}",
        ]
    if target.harness == "copilot" and target.account == "default":
        return [
            "copilot",
            "-p",
            PROMPT,
            *model,
            "--silent",
            "--stream",
            "off",
            "--available-tools",
            "",
            "--disable-builtin-mcps",
            "--no-custom-instructions",
            "--no-remote-export",
            "--log-level",
            "none",
        ]
    return []


def _run(args, *, input, cwd, env, timeout):
    with subprocess.Popen(
        args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        env=env,
        text=True,
        start_new_session=True,
    ) as child:
        try:
            stdout, stderr = child.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.communicate()
            return subprocess.CompletedProcess(args, 124, "", "")
        return subprocess.CompletedProcess(args, child.returncode, stdout, stderr)


def _frames(output: str) -> list:
    content = "\n".join(
        line for line in output.splitlines() if not line.startswith(("[agenti] ", "[agentihooks codex] "))
    ).strip()
    decoder = json.JSONDecoder()
    rows = []
    while content:
        row, end = decoder.raw_decode(content)
        rows.append(row)
        content = content[end:].lstrip()
    return rows


def _succeeded(harness: str, output: str) -> bool:
    if harness == "copilot":
        return output.strip() == "OK"
    try:
        rows = _frames(output)
        if harness == "claude":
            return any(row.get("type") == "result" and not row.get("is_error") and row.get("result") for row in rows)
        completed = any(row.get("type") == "turn.completed" for row in rows)
        answered = any(
            row.get("type") == "item.completed"
            and row.get("item", {}).get("type") == "agent_message"
            and row["item"].get("text")
            for row in rows
        )
        return completed and answered
    except (ValueError, AttributeError, TypeError):
        return False


def check(target: Target, directory: Path, run: Callable = _run) -> Result:
    from hooks.context.account_sessions import INTERNAL_RUN

    args = _arguments(target) if target.account else []
    if not args:
        return _result(False, "Provider account route unavailable")
    directory.mkdir(parents=True, exist_ok=True)
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("AGENTIHOOKS_SWARM", "AGENTIHOOKS_AGENT_", "AGENTIHOOKS_PROFILE_"))
        and name not in ("CLAUDECODE", "AGENTIHOOKS_SESSION_ID")
    }
    env.update({"AGENTIHOOKS_CLASSIFIER_CHILD": "1", INTERNAL_RUN: "1"})
    try:
        result = run(args, input=PROMPT, cwd=directory, env=env, timeout=TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _result(False, f"Provider request failed: {type(exc).__name__}")
    ok = result.returncode == 0 and _succeeded(target.harness, result.stdout)
    return _result(ok, "Provider request succeeded" if ok else f"Provider request failed: status {result.returncode}")
