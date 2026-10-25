from __future__ import annotations

import json
import os
import re
import shlex
import tomllib
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from scripts.codex_session.binding import TransportRecord
from scripts.swarm.health.findings import Finding
from scripts.swarm.pane import PaneObservation
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

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
LAUNCH_KEYS = ("profile", "harness", "model", "effort")
RECORDED = (*LAUNCH_KEYS, "account")


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
    from hooks.context import profile_chain
    from scripts.profiles import binding
    from scripts.select_profile import _native_options
    from scripts.swarm import launch_check, launch_model

    try:
        found, harness, env, account = binding.process(proc, pid)
        argv = (proc / str(found) / "cmdline").read_bytes().decode(errors="replace").split("\0")
        model, effort, _ = _native_options(harness, argv[1:])
        home = env.get(binding.HOMES[harness])
        config = launch_model.configured(harness, Path(home)) if home else {}
        model, effort = model or config.get("model", ""), effort or config.get("effort", "")
        return {
            "harness": harness,
            "home": str(Path(home).resolve()) if home else "",
            "profile": env.get("AGENTIHOOKS_PROFILE", ""),
            "model": model,
            "effort": effort,
            "account": account,
            "hooks": bool(home) and hooks(Path(home), harness),
            "chain": launch_check.chain(home) if home else [],
            "overlays": profile_chain.rendered_overlays(home) if home else [],
        }
    except (OSError, ValueError, StopIteration):
        return {"process": False}


def managed(agent: AgentRecord) -> bool:
    from scripts.swarm.store import APP_SERVER_TRANSPORT

    return agent.runtime_backend == "local" and getattr(agent, "transport", None) == APP_SERVER_TRANSPORT


def transport(store: RedisStore, agent: AgentRecord) -> TransportRecord | None:
    from scripts.codex_session.binding import BindingStore

    slug = store.names.slug_of(agent.name)
    current = store.execution_occupants(slug).get(agent.seat)
    if current is None or current.execution_id != agent.execution_id:
        return None
    record = BindingStore(store).get(slug, agent.execution_id)
    if record is None or (record.execution_generation, record.agent_name, record.seat) != (
        agent.generation,
        agent.name,
        agent.seat,
    ):
        return None
    return record


def server_alive(record: TransportRecord | None) -> bool | None:
    from scripts.codex_session import supervisor
    from scripts.codex_session.binding import EXITED, FAILED

    if record is None:
        return None
    if record.state in {EXITED, FAILED}:
        return False
    if not record.server_pid or not record.server_start_identity:
        return None
    return supervisor.owns(record, supervisor.start_time)


def observe_managed(record: TransportRecord | None) -> PaneObservation:
    from scripts.codex_session import protocol
    from scripts.codex_session.supervisor import UNIX

    alive = server_alive(record)
    if alive is False:
        return PaneObservation("dead")
    if alive is None or not record.thread_id:
        return PaneObservation("unknown")
    try:
        connection, _ = protocol.connect(record.endpoint_ref.removeprefix(UNIX), 1.0)
        try:
            thread = protocol.read_thread(connection, record.thread_id)
        finally:
            connection.close()
    except (OSError, TimeoutError, protocol.ProtocolError):
        return PaneObservation("unknown")
    if thread.get("id") != record.thread_id:
        return PaneObservation("unknown")
    turns = thread.get("turns") or [{}]
    if record.pending_request:
        return PaneObservation("waiting")
    return PaneObservation("working" if turns[-1].get("status") == "inProgress" else "idle")


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


def relaunch_assignment(agent: AgentRecord, task: dict, config: SwarmConfig) -> dict:
    from scripts.swarm.templates import DEFAULT_PROFILES

    saved = {
        **assignment(agent),
        "seat": agent.seat,
        "overlays": agent.overlays,
        "bundle_revision": agent.profile_decision.get("bundle_revision", ""),
    }
    if not saved["profile"]:
        saved["profile"] = (
            task.get("profile") or config.lanes.get(agent.lane, {}).get("profile") or DEFAULT_PROFILES[agent.lane]
        )
    return saved


def complete(saved: dict | None) -> dict:
    return saved if saved and all(saved.get(key) for key in LAUNCH_KEYS) else {}


def fill(agent: AgentRecord, facts: dict) -> AgentRecord:
    if agent.harness:
        return agent
    found = {key: facts[key] for key in RECORDED if not getattr(agent, key) and facts.get(key)}
    return replace(agent, **found) if found else agent


def bound_session(agent: AgentRecord, sessions: list[Session]) -> Session | None:
    named = {s.process.pid: s for s in sessions if s.name == agent.name}
    validated = agent.profile_decision.get("validation", {}).get("pid")
    conversation = [s for s in named.values() if agent.conversation_id and s.session_id == agent.conversation_id]
    if validated:
        resumed = [s for s in conversation if s.status == "alive"]
        return named.get(validated) or min(resumed, key=lambda s: s.process.start_time, default=None)
    if conversation:
        return conversation[0]
    registered = [s for s in named.values() if s.status != "unregistered" and s.target == agent.harness]
    return min(registered or named.values(), key=lambda s: s.process.start_time, default=None)


def _unheld(agent: AgentRecord) -> list[str]:
    homeless = not agent.profile_decision.get("validation", {}).get("home") and not (agent.profile and agent.harness)
    return [field for field, value in assignment(agent).items() if not value or (field == "home" and homeless)]


def unknown(agent: AgentRecord, facts: dict) -> list[str]:
    return [field for field in _unheld(agent) if field in RECORDED and facts.get(field)]


def compare(agent: AgentRecord, facts: dict) -> dict:
    if facts.get("process") is False:
        return {"process": {"expected": True, "actual": False}}
    expected = {**assignment(agent), "hooks": True}
    missing = _unheld(agent)
    return {
        field: {"expected": value, "actual": facts.get(field)}
        for field, value in expected.items()
        if field not in missing and facts.get(field) != value
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
    if "pane" not in facts and facts.get("process") is not False:
        report["unknown"] = unknown(agent, facts)
    store.redis.hset(store.key(slug, "live-bindings"), agent.name, json.dumps(report))
    return differences


def _seated(agents: list[AgentRecord]) -> set[str]:
    newest = {}
    for agent in agents:
        if agent.started_at >= newest.get(agent.task, agent).started_at:
            newest[agent.task] = agent
    return {agent.name for agent in newest.values() if agent.state != "finished"}


def findings(store: RedisStore, slug: str) -> list[Finding]:
    result = []
    seated = _seated(store.agents(slug))
    for raw in store.redis.hgetall(store.key(slug, "live-bindings")).values():
        report = json.loads(raw)
        if report["agent"] not in seated:
            continue
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
        for field in report.get("unknown", []):
            result.append(
                Finding(
                    "live binding",
                    f"{report['agent']}/{field}",
                    f"{report['agent']} has no recorded {field}",
                    (f"the record never held {field}, so it is not compared and never retires the agent",),
                    "assigned launch must be recorded",
                    1,
                )
            )
    return result
