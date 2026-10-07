from __future__ import annotations

import json
import os
import re
import shlex
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

from scripts.swarm.health.findings import Finding
from scripts.swarm.store import AgentRecord, RedisStore

if TYPE_CHECKING:
    from scripts.terminate_agent import Session

EVENTS = (
    "SessionStart",
    "SessionEnd",
    "PreToolUse",
    "PostToolUse",
    "Stop",
    "UserPromptSubmit",
    "SubagentStart",
    "SubagentStop",
    "PreCompact",
    "PermissionRequest",
)


def lifecycle_command(command: str) -> bool:
    words = shlex.split(command)
    if words[:1] == ["cd"]:
        if len(words) < 4 or words[2] != "&&" or not Path(words[1]).is_dir():
            return False
        words = words[3:]
    if words[:1] == ["exec"]:
        words = words[1:]
    if not words:
        return False
    program = Path(words[0]).name
    if program in {"bash", "sh"}:
        words = words[1:]
        return bool(words) and Path(words[0]).name == "agentihooks-hook.sh" and Path(words[0]).is_file()
    if program == "agentihooks-hook.sh":
        return Path(words[0]).is_file() and os.access(words[0], os.X_OK)
    return bool(re.fullmatch(r"python(?:\d(?:\.\d+)*)?", program)) and words[1:3] == ["-m", "hooks"]


def hooks(home: Path, harness: str) -> bool:
    try:
        path = home / ("hooks.json" if harness == "codex" else "settings.json")
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or data.get("disableAllHooks"):
            return False
        if harness == "codex":
            config = tomllib.loads((home / "config.toml").read_text())
            if config["features"]["hooks"] is not True:
                return False
        registered = data["hooks"]
        return all(
            any(
                hook["type"] == "command" and lifecycle_command(hook["command"])
                for group in registered[event]
                for hook in group["hooks"]
            )
            for event in EVENTS
        )
    except (OSError, ValueError, AttributeError, TypeError, KeyError):
        return False


def read(agent: AgentRecord, pid: int, proc: Path = Path("/proc")) -> dict:
    from scripts.profiles import binding
    from scripts.select_profile import _native_options

    try:
        found, harness, env, account = binding.process(proc, pid)
        argv = (proc / str(found) / "cmdline").read_bytes().decode(errors="replace").split("\0")
        model, effort, _ = _native_options(harness, argv[1:])
        home = env.get(binding.HOMES[harness])
        return {
            "harness": harness,
            "home": str(Path(home).resolve()) if home else "",
            "profile": env.get("AGENTIHOOKS_PROFILE", ""),
            "model": model,
            "effort": effort,
            "account": account,
            "hooks": bool(home) and hooks(Path(home), harness),
        }
    except (OSError, ValueError, StopIteration):
        return {"process": False}


def assignment(agent: AgentRecord) -> dict:
    validated = agent.profile_decision.get("validation", {})
    home = validated.get("home") or str(Path.home() / ".agentihooks" / "profiles" / agent.profile / agent.harness)
    return {
        "harness": agent.harness,
        "home": str(Path(home).resolve()),
        "profile": agent.profile,
        "model": validated.get("model") or agent.model,
        "effort": validated.get("effort") or agent.effort,
        "account": agent.account,
    }


def bound_session(agent: AgentRecord, sessions: list[Session]) -> Session | None:
    named = {s.process.pid: s for s in sessions if s.name == agent.name}
    validated = agent.profile_decision.get("validation", {}).get("pid")
    if validated:
        return named.get(validated)
    if agent.conversation_id:
        conversation = [s for s in named.values() if s.session_id == agent.conversation_id]
        if conversation:
            return conversation[0]
    registered = [s for s in named.values() if s.status != "unregistered" and s.target == agent.harness]
    return min(registered or named.values(), key=lambda s: s.process.start_time, default=None)


def compare(agent: AgentRecord, facts: dict) -> dict:
    if facts.get("process") is False:
        return {"process": {"expected": True, "actual": False}}
    expected = {**assignment(agent), "hooks": True}
    return {
        field: {"expected": value, "actual": facts.get(field)}
        for field, value in expected.items()
        if facts.get(field) != value
    }


def record(store: RedisStore, slug: str, agent: AgentRecord, facts: dict, now_ms: int) -> dict:
    differences = (
        {"pane": {"expected": "closed", "actual": facts["pane"]}} if "pane" in facts else compare(agent, facts)
    )
    report = {
        "agent": agent.name,
        "at": now_ms,
        "state": "mismatched" if differences else "matching",
        "differences": differences,
    }
    store.redis.hset(store.key(slug, "live-bindings"), agent.name, json.dumps(report))
    return differences


def findings(store: RedisStore, slug: str) -> list[Finding]:
    result = []
    for raw in store.redis.hgetall(store.key(slug, "live-bindings")).values():
        report = json.loads(raw)
        for field, values in report["differences"].items():
            result.append(
                Finding(
                    "live binding",
                    f"{report['agent']}/{field}",
                    f"{report['agent']} differs in {field}",
                    (f"expected {values['expected']}; observed {values['actual']}",),
                    "assigned launch must match",
                    1,
                )
            )
    return result
