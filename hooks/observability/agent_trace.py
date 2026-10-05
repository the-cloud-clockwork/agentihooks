"""One Langfuse trace per agent session, rebuilt from the transcript on every Stop."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from hooks.config import AGENTIHOOKS_HOME

CURSOR_DIR = AGENTIHOOKS_HOME / "agent_trace"
SYSTEM = "anthropic"
_EXPORTER_LOGGER = "opentelemetry"


@dataclass(frozen=True)
class Identity:
    session_id: str
    agent: str = ""
    swarm: str = ""
    lane: str = ""
    task: str = ""
    account: str = ""

    def tags(self) -> tuple[str, ...]:
        pairs = (
            ("swarm", self.swarm),
            ("agent", self.agent),
            ("lane", self.lane),
            ("task", self.task),
            ("account", self.account),
        )
        return tuple(f"{key}:{value}" for key, value in pairs if value)


@dataclass
class SpanSpec:
    name: str
    span_id: int
    parent_id: int | None
    start_ns: int
    end_ns: int
    attributes: dict = field(default_factory=dict)


def identity_from_env(session_id: str, environ: Mapping[str, str] | None = None) -> Identity:
    from hooks.context.account_sessions import UNROUTED, environment_account

    env = os.environ if environ is None else environ
    account = environment_account(env)
    return Identity(
        session_id=session_id,
        agent=env.get("AGENTIHOOKS_AGENT_NAME", ""),
        swarm=env.get("AGENTIHOOKS_SWARM", ""),
        lane=env.get("AGENTIHOOKS_SWARM_LANE", ""),
        task=env.get("AGENTIHOOKS_SWARM_TASK", ""),
        account="" if account == UNROUTED else account,
    )


def _digest(*parts: str) -> bytes:
    return hashlib.sha256("\0".join(parts).encode()).digest()


def trace_id(session_id: str) -> int:
    return int.from_bytes(_digest("trace", session_id)[:16], "big") or 1


def _span_id(session_id: str, key: str) -> int:
    return int.from_bytes(_digest("span", session_id, key)[:8], "big") or 1


def _ns(entry: dict) -> int:
    try:
        return int(datetime.fromisoformat(entry["timestamp"].replace("Z", "+00:00")).timestamp() * 1e9)
    except (KeyError, AttributeError, ValueError):
        return 0


def _blocks(entry: dict) -> list:
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return content if isinstance(content, list) else []


def _is_prompt(entry: dict) -> bool:
    if entry.get("type") != "user" or entry.get("isMeta"):
        return False
    message = entry.get("message")
    if isinstance(message, dict) and isinstance(message.get("content"), str):
        return True
    return not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in _blocks(entry))


def turns(entries: list[dict]) -> list[list[dict]]:
    result: list[list[dict]] = []
    for entry in entries:
        if entry.get("isSidechain") or entry.get("type") not in ("user", "assistant") or not _ns(entry):
            continue
        if _is_prompt(entry):
            result.append([entry])
        elif result:
            result[-1].append(entry)
    return result


def _usage_attributes(usage: dict) -> dict:
    keys = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    return {f"gen_ai.usage.{key}": int(usage.get(key) or 0) for key in keys}


def _generations(turn: list[dict]) -> dict[str, tuple[int, dict]]:
    """message id -> (start ns, last entry carrying it); start is the entry before its first block."""
    found: dict[str, tuple[int, dict]] = {}
    previous = _ns(turn[0])
    for entry in turn:
        message = entry.get("message")
        if entry.get("type") == "assistant" and isinstance(message, dict) and message.get("id"):
            start = found[message["id"]][0] if message["id"] in found else previous
            found[message["id"]] = (start, entry)
        previous = _ns(entry)
    return found


def _turn_spans(session_id: str, number: int, turn: list[dict], root: int) -> list[SpanSpec]:
    turn_id = _span_id(session_id, turn[0].get("uuid", str(number)))
    spans = [
        SpanSpec(
            f"turn {number}",
            turn_id,
            root,
            _ns(turn[0]),
            _ns(turn[-1]),
            {"langfuse.observation.type": "span", "agent.turn": number},
        )
    ]
    for message_id, (start, entry) in _generations(turn).items():
        message = entry["message"]
        model = message.get("model", "")
        attributes = {
            "langfuse.observation.type": "generation",
            "gen_ai.operation.name": "chat",
            "gen_ai.system": SYSTEM,
            "gen_ai.request.model": model,
            "gen_ai.response.model": model,
            "gen_ai.response.id": message_id,
            **_usage_attributes(message.get("usage") or {}),
        }
        spans.append(
            SpanSpec(model or "generation", _span_id(session_id, message_id), turn_id, start, _ns(entry), attributes)
        )
    results = {
        b.get("tool_use_id"): (entry, b)
        for entry in turn
        for b in _blocks(entry)
        if isinstance(b, dict) and b.get("type") == "tool_result"
    }
    for entry in turn:
        for block in _blocks(entry):
            if entry.get("type") != "assistant" or not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            done, result = results.get(block.get("id"), (entry, {}))
            attributes = {
                "langfuse.observation.type": "tool",
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": block.get("name", ""),
                "gen_ai.tool.call.id": block.get("id", ""),
                "error": bool(result.get("is_error")),
            }
            span_id = _span_id(session_id, block.get("id", ""))
            spans.append(SpanSpec(block.get("name", "tool"), span_id, turn_id, _ns(entry), _ns(done), attributes))
    return spans


def session_spans(
    entries: list[dict], identity: Identity, cost: float | None = None, first_turn: int = 0
) -> list[SpanSpec]:
    all_turns = turns(entries)
    if not all_turns:
        return []
    session_id = identity.session_id
    root_id = _span_id(session_id, "root")
    totals = {"gen_ai.usage.input_tokens": 0, "gen_ai.usage.output_tokens": 0}
    model = ""
    for turn in all_turns:
        for _, entry in _generations(turn).values():
            usage = _usage_attributes(entry["message"].get("usage") or {})
            for key in totals:
                totals[key] += usage[key]
            model = entry["message"].get("model") or model
    name = identity.agent or "agent-session"
    attributes = {
        "langfuse.observation.type": "agent",
        "langfuse.trace.name": name,
        "langfuse.session.id": session_id,
        "langfuse.trace.tags": identity.tags(),
        "gen_ai.operation.name": "invoke_agent",
        "gen_ai.system": SYSTEM,
        "gen_ai.agent.name": name,
        "gen_ai.conversation.id": session_id,
        "gen_ai.request.model": model,
        "agent.turns": len(all_turns),
        **totals,
    }
    if cost is not None:
        attributes["gen_ai.usage.cost"] = float(cost)
    spans = [SpanSpec(name, root_id, None, _ns(all_turns[0][0]), _ns(all_turns[-1][-1]), attributes)]
    for number, turn in enumerate(all_turns[first_turn:], start=first_turn + 1):
        spans.extend(_turn_spans(session_id, number, turn, root_id))
    return spans


def read_entries(transcript_path: str) -> list[dict]:
    entries = []
    with open(transcript_path, encoding="utf-8") as handle:
        for line in handle:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict):
                entries.append(entry)
    if any(entry.get("type") == "session_meta" for entry in entries):
        from hooks.observability.codex_transcript import normalize_entries

        return normalize_entries(entries)
    return entries


def _cursor_path(session_id: str) -> Path:
    safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id)
    return CURSOR_DIR / f"{safe_id}.json"


def _exported_turns(session_id: str) -> int:
    try:
        return int(json.loads(_cursor_path(session_id).read_text(encoding="utf-8"))["turns"])
    except (OSError, ValueError, TypeError, KeyError):
        return 0


def _readable(spec: SpanSpec, trace: int):
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.trace import SpanContext, SpanKind, TraceFlags

    from hooks.config import OTEL_HOOKS_SERVICE_NAME

    def context(span_id: int) -> SpanContext:
        return SpanContext(trace, span_id, is_remote=False, trace_flags=TraceFlags(TraceFlags.SAMPLED))

    return ReadableSpan(
        name=spec.name,
        context=context(spec.span_id),
        parent=context(spec.parent_id) if spec.parent_id else None,
        resource=Resource.create({"service.name": OTEL_HOOKS_SERVICE_NAME}),
        attributes=spec.attributes,
        kind=SpanKind.INTERNAL,
        start_time=spec.start_ns,
        end_time=max(spec.end_ns, spec.start_ns),
    )


class _ExportErrors(logging.Handler):
    """Collects the HTTP status and reason the OTLP exporter logs when it gives up."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.status: object = None
        self.reason = "exporter returned failure"

    def emit(self, record: logging.LogRecord) -> None:
        found = re.search(r"code: (\S+), reason: (.*)", record.getMessage(), re.S)
        if found:
            self.status, self.reason = found.groups()
        elif record.levelno >= logging.ERROR:
            self.reason = record.getMessage()


def _log_failure(status: object, reason: str) -> None:
    from hooks.observability import otel

    endpoint = (otel.langfuse_exporter_config() or {}).get("endpoint", "")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sys.stderr.write(f"{stamp} agent_trace export failed endpoint={endpoint} status={status} reason={reason}\n")


def export_session(session_id: str, transcript_path: str, identity: Identity | None = None) -> None:
    from opentelemetry.sdk.trace.export import SpanExportResult

    from hooks.context.context_usage import session_cost
    from hooks.observability import otel

    exporter = otel.langfuse_exporter()
    if exporter is None or not session_id or not transcript_path:
        return
    identity = identity or identity_from_env(session_id)
    entries = read_entries(transcript_path)
    exported = _exported_turns(session_id)
    spans = session_spans(entries, identity, session_cost(session_id), first_turn=exported)
    if not spans:
        return
    trace = trace_id(session_id)
    errors = _ExportErrors()
    logging.getLogger(_EXPORTER_LOGGER).addHandler(errors)
    try:
        result = exporter.export([_readable(spec, trace) for spec in spans])
    except Exception as e:  # noqa: BLE001
        errors.reason = f"{type(e).__name__}: {e}"
        result = SpanExportResult.FAILURE
    finally:
        logging.getLogger(_EXPORTER_LOGGER).removeHandler(errors)
        exporter.shutdown()
    if result is not SpanExportResult.SUCCESS:
        _log_failure(errors.status, errors.reason)
        return
    path = _cursor_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"turns": max(exported, len(turns(entries)))}), encoding="utf-8")
