"""One correlation envelope shared by the Langfuse agent trace and the collector logs and gauges."""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

SCHEMA = "agentihooks.correlation/1"
PREFIX = "agentihooks.correlation."
PRESENT, MISSING, UNSUPPORTED = "present", "missing", "unsupported"
CACHE_TTL_SEC = 60
KEYS = (
    "AGENTIHOOKS_SWARM",
    "AGENTIHOOKS_AGENT_NAME",
    "AGENTIHOOKS_SWARM_TASK",
    "AGENTIHOOKS_PROFILE",
    "AGENTIHOOKS_PROFILE_REPORT",
    "AGENTIHOOKS_TARGET",
    "AGENTIHOOKS_PREDECESSOR_SESSION",
)

SOURCES = {
    "ledger": "env AGENTIHOOKS_SWARM",
    "task": "env AGENTIHOOKS_SWARM_TASK",
    "phase": "ledger task row phase",
    "seat": "AgentRecord.seat",
    "agent.name": "env AGENTIHOOKS_AGENT_NAME",
    "agent.started_at": "AgentRecord.started_at",
    "agent.life": "agent.name and AgentRecord.started_at",
    "session.id": "hook session_id",
    "trace.id": "agent_trace.trace_id(session_id)",
    "conversation.id": "AgentRecord.conversation_id",
    "harness": "hook target AGENTIHOOKS_TARGET",
    "profile.requested": "env AGENTIHOOKS_PROFILE",
    "profile.validation": "launch profile report state",
    "profile.resolved": "launch profile report validation.profile, only when validated",
    "profile.source": "AgentRecord.profile_decision.source",
    "classifier.model": "AgentRecord.profile_decision.model",
    "classifier.confidence": "AgentRecord.profile_decision.confidence",
    "model": "AgentRecord.model",
    "model.source": "AgentRecord.model_source",
    "model.confidence": "AgentRecord.model_confidence",
    "effort": "AgentRecord.effort",
    "account": "routed token variable name",
    "revision": "launch profile report validation.revisions",
    "revision.sources": "launch profile report validation.sources",
    "predecessor.session.id": "env AGENTIHOOKS_PREDECESSOR_SESSION, set by a transfer launch",
    "predecessor.trace.id": "agent_trace.trace_id(predecessor session id)",
}


@dataclass(frozen=True)
class Inputs:
    """None marks a source this session does not have; an empty dict marks one that could not be read."""

    session_id: str
    environ: Mapping[str, str]
    harness: str
    agent: dict | None = None
    report: dict | None = None
    task: dict | None = None


def _value(value: object, available: bool = True) -> tuple[str, object]:
    if not available:
        return UNSUPPORTED, None
    if value is None or value == "":
        return MISSING, None
    return PRESENT, value


def _revision(revisions: object) -> str:
    if not isinstance(revisions, dict):
        return ""
    return ",".join(sorted(f"{Path(repo).name}@{sha}" for repo, sha in revisions.items() if sha))


def envelope(inputs: Inputs) -> dict[str, tuple[str, object]]:
    from hooks.context.account_sessions import UNROUTED, environment_account
    from hooks.observability.agent_trace import trace_id

    env = inputs.environ
    swarm = bool(env.get("AGENTIHOOKS_SWARM"))
    agent = inputs.agent if inputs.agent is not None else {}
    decision = agent.get("profile_decision") if isinstance(agent.get("profile_decision"), dict) else {}
    report = inputs.report if inputs.report is not None else {}
    validated = report.get("validation") if report.get("state") == "validated" else None
    validated = validated if isinstance(validated, dict) else {}
    name = env.get("AGENTIHOOKS_AGENT_NAME")
    started = agent.get("started_at")
    account = environment_account(env)
    has_agent, has_report = inputs.agent is not None, inputs.report is not None
    session = inputs.session_id
    predecessor = env.get("AGENTIHOOKS_PREDECESSOR_SESSION")
    return {
        "ledger": _value(env.get("AGENTIHOOKS_SWARM"), swarm),
        "task": _value(env.get("AGENTIHOOKS_SWARM_TASK"), swarm),
        "phase": _value((inputs.task or {}).get("phase"), inputs.task is not None),
        "seat": _value(agent.get("seat"), has_agent),
        "agent.name": _value(name),
        "agent.started_at": _value(started if isinstance(started, int) and started else None, has_agent),
        "agent.life": _value(f"{name}#{started}" if name and isinstance(started, int) and started else None, has_agent),
        "session.id": _value(session),
        "trace.id": _value(format(trace_id(session), "032x") if session else None),
        "conversation.id": _value(agent.get("conversation_id"), has_agent),
        "harness": _value(inputs.harness),
        "profile.requested": _value(env.get("AGENTIHOOKS_PROFILE")),
        "profile.validation": _value(report.get("state"), has_report),
        "profile.resolved": _value(validated.get("profile"), has_report),
        "profile.source": _value(decision.get("source"), has_agent),
        "classifier.model": _value(decision.get("model"), has_agent),
        "classifier.confidence": _value(_number(decision.get("confidence")), has_agent),
        "model": _value(agent.get("model"), has_agent),
        "model.source": _value(agent.get("model_source"), has_agent),
        "model.confidence": _value(_number(agent.get("model_confidence")), has_agent),
        "effort": _value(agent.get("effort"), has_agent),
        "account": _value("" if account == UNROUTED else account),
        "revision": _value(_revision(validated.get("revisions")), has_report),
        "revision.sources": _value(validated.get("sources"), has_report),
        "predecessor.session.id": _value(predecessor),
        "predecessor.trace.id": _value(format(trace_id(predecessor), "032x") if predecessor else None),
    }


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def attributes(fields: Mapping[str, tuple[str, object]]) -> dict[str, object]:
    flat: dict[str, object] = {f"{PREFIX}schema": SCHEMA}
    for name, (state, value) in fields.items():
        if state == PRESENT:
            flat[f"{PREFIX}{name}"] = value
        else:
            flat[f"{PREFIX}{name}.state"] = state
    return flat


def _agent_record(slug: str, name: str) -> dict:
    from scripts.swarm.store import connect

    try:
        store = connect()
        raw = store.redis.hget(store.key(slug, "agents"), name)
        return json.loads(raw) if raw else {}
    except Exception:  # noqa: BLE001
        return {}


def _ledger_task(slug: str, task: str) -> dict:
    from scripts.swarm_ledger.repository.folder import ledger_folder
    from scripts.swarm_ledger.repository.sqlite import read_ledger

    return ((read_ledger(ledger_folder(os.environ), slug, f"tasks/{task}") or {}).get("tasks") or [{}])[0]


def _report(path: str) -> dict:
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def gather(session_id: str, environ: Mapping[str, str]) -> Inputs:
    from hooks.targets import DEFAULT_TARGET

    slug, name, task = (environ.get(key) for key in KEYS[:3])
    report = environ.get("AGENTIHOOKS_PROFILE_REPORT")
    return Inputs(
        session_id=session_id,
        environ=environ,
        harness=(environ.get("AGENTIHOOKS_TARGET") or DEFAULT_TARGET).strip().lower(),
        agent=(_agent_record(slug, name) if name else {}) if slug else None,
        report=_report(report) if report else None,
        task=(_ledger_task(slug, task) if task else {}) if slug else None,
    )


def resolve(session_id: str, environ: Mapping[str, str] | None = None) -> dict[str, object]:
    """Cache envelope attributes while the launch environment and profile validation report are unchanged."""
    from hooks import config
    from hooks.context.account_sessions import environment_account
    from hooks.observability.signals import safe_name

    env = os.environ if environ is None else environ
    launch = [
        session_id,
        environment_account(env),
        *(env.get(key) for key in KEYS),
        _report(env["AGENTIHOOKS_PROFILE_REPORT"]) if env.get("AGENTIHOOKS_PROFILE_REPORT") else None,
    ]
    path = config.AGENTIHOOKS_HOME / "telemetry" / "correlation" / f"{safe_name(session_id)}.json"
    try:
        cached = json.loads(path.read_text())
        if cached["launch"] == launch and time.time() < cached["at"] + CACHE_TTL_SEC:
            return cached["attributes"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    flat = attributes(envelope(gather(session_id, env)))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp = tempfile.mkstemp(dir=path.parent)
        with os.fdopen(handle, "w") as out:
            json.dump({"launch": launch, "at": time.time(), "attributes": flat}, out)
        os.replace(temp, path)
    except OSError:
        pass
    return flat
