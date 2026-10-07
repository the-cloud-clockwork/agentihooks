"""One Langfuse trace per agent session, rebuilt from the transcript on every Stop."""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from hooks.config import AGENTIHOOKS_HOME

CURSOR_DIR = AGENTIHOOKS_HOME / "agent_trace"
SYSTEM = "anthropic"
_EXPORTER_LOGGER = "opentelemetry"
BATCH_CHARS = 2_000_000
PENDING_MAX_BYTES = 8_000_000
TRIGGER_ENV = "AGENTIHOOKS_TRACE_FLUSH_TRIGGER"


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

    def user_id(self) -> str:
        return self.account or os.environ.get("USER", "")


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


def _field(text: str) -> str:
    return _field_info(text)[0]


def _field_info(text: str) -> tuple[str, int]:
    from hooks import config
    from hooks.secrets import redact

    text = redact(text, mode="strict")
    cap = config.LANGFUSE_FIELD_MAX_CHARS
    removed = max(0, len(text) - cap)
    return (f"{text[:cap]}…[truncated {removed} chars]" if removed else text), removed


def _fields(fields: dict[str, str]) -> dict:
    result = {}
    for key, text in fields.items():
        if not text:
            continue
        value, removed = _field_info(text)
        result[key] = value
        if removed:
            result[f"agentihooks.truncation.{key}.chars"] = removed
    return result


def _io(input_text: str = "", output_text: str = "") -> dict:
    return _fields({"langfuse.observation.input": input_text, "langfuse.observation.output": output_text})


def _text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    labels = {"text": lambda b: b.get("text", ""), "image": lambda b: "[image]"}
    return "\n".join(labels[b["type"]](b) for b in content if isinstance(b, dict) and b.get("type") in labels)


def _prompt_text(turn: list[dict]) -> str:
    message = turn[0].get("message")
    return _text(message.get("content")) if isinstance(message, dict) else ""


def _assistant_text(entries: list[dict]) -> str:
    texts = [
        b.get("text", "")
        for entry in entries
        if entry.get("type") == "assistant"
        for b in _blocks(entry)
        if isinstance(b, dict) and b.get("type") == "text"
    ]
    return "\n\n".join(t for t in texts if t)


def _turn_output(turn: list[dict], carry: Mapping | None) -> str:
    earlier = carry["text"] if carry else ""
    return "\n\n".join(text for text in (earlier, _assistant_text(turn)) if text)


def _generations(turn: list[dict], previous: int | None = None) -> dict[str, tuple[int, dict]]:
    """message id -> (start ns, last entry carrying it); start is the entry before its first block."""
    found: dict[str, tuple[int, dict]] = {}
    previous = _ns(turn[0]) if previous is None else previous
    for entry in turn[1:]:
        message = entry.get("message")
        if entry.get("type") == "assistant" and isinstance(message, dict) and message.get("id"):
            start = found[message["id"]][0] if message["id"] in found else previous
            found[message["id"]] = (start, entry)
        previous = _ns(entry)
    return found


def _outcome(result: dict) -> dict:
    if not result:
        return {"tool.outcome.state": "missing"}
    return {"tool.outcome": "error" if result.get("is_error") else "success"}


def _turn_spans(
    session_id: str,
    number: int,
    turn: list[dict],
    root: int,
    results: dict | None = None,
    carry: Mapping | None = None,
) -> list[SpanSpec]:
    carry = carry or {}
    turn_id = _span_id(session_id, turn[0].get("uuid", str(number)))
    output = _turn_output(turn, carry)
    spans = [
        SpanSpec(
            f"turn {number}",
            turn_id,
            root,
            _ns(turn[0]),
            _ns(turn[-1]),
            {
                "langfuse.observation.type": "span",
                "agent.turn": number,
                **_io(_prompt_text(turn), output),
            },
        )
    ]
    for message_id, (start, entry) in _generations(turn, carry.get("previous_ns")).items():
        message = entry["message"]
        model = message.get("model", "")
        same_message = [e for e in turn if isinstance(e.get("message"), dict) and e["message"].get("id") == message_id]
        attributes = {
            "langfuse.observation.type": "generation",
            "gen_ai.operation.name": "chat",
            "gen_ai.system": SYSTEM,
            "gen_ai.request.model": model,
            "gen_ai.response.model": model,
            "gen_ai.response.id": message_id,
            **_usage_attributes(message.get("usage") or {}),
            **_io(output_text=_assistant_text(same_message)),
        }
        spans.append(
            SpanSpec(model or "generation", _span_id(session_id, message_id), turn_id, start, _ns(entry), attributes)
        )
    results = results or {
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
                **_outcome(result),
                **_io(json.dumps(block.get("input", {}), ensure_ascii=False), _text(result.get("content"))),
            }
            span_id = _span_id(session_id, block.get("id", ""))
            spans.append(SpanSpec(block.get("name", "tool"), span_id, turn_id, _ns(entry), _ns(done), attributes))
    return spans


def session_spans(
    entries: list[dict],
    identity: Identity,
    cost: float | None = None,
    first_turn: int = 0,
    root: Mapping[str, object] | None = None,
    paged: Mapping | None = None,
) -> list[SpanSpec]:
    all_turns = turns(entries)
    if not all_turns:
        return []
    paged = paged or {}
    session_id = identity.session_id
    root_id = _span_id(session_id, "root")
    totals = {
        "gen_ai.usage.input_tokens": paged.get("input_tokens", 0),
        "gen_ai.usage.output_tokens": paged.get("output_tokens", 0),
    }
    model = paged.get("model", "")
    for turn in all_turns:
        for _, entry in _generations(turn).values():
            usage = _usage_attributes(entry["message"].get("usage") or {})
            for key in totals:
                totals[key] += usage[key]
            model = entry["message"].get("model") or model
    name = identity.agent or "agent-session"
    offset = paged.get("turns", 0)
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
        "agent.turns": offset + len(all_turns),
        **totals,
    }
    if cost is not None:
        attributes["gen_ai.usage.cost"] = float(cost)
    attributes.update(root or {})
    trace_io = {
        "langfuse.trace.input": paged.get("input", _prompt_text(all_turns[0])),
        "langfuse.trace.output": _turn_output(all_turns[-1], paged.get("open") if len(all_turns) == 1 else None),
    }
    attributes.update(_fields(trace_io))
    start = paged.get("start_ns") or _ns(all_turns[0][0])
    spans = [SpanSpec(name, root_id, None, start, _ns(all_turns[-1][-1]), attributes)]
    results = _results(entries)
    for number, turn in enumerate(all_turns[first_turn:], start=offset + first_turn + 1):
        carry = paged.get("open") if number == offset + 1 else None
        spans.extend(_turn_spans(session_id, number, turn, root_id, results, carry))
    shared = {"langfuse.session.id": session_id, "langfuse.user.id": identity.user_id()}
    for span in spans:
        span.attributes.update({key: value for key, value in shared.items() if value})
    return spans


def _results(entries: list[dict]) -> dict:
    return {
        block.get("tool_use_id"): (entry, block)
        for entry in entries
        for block in _blocks(entry)
        if isinstance(block, dict) and block.get("type") == "tool_result"
    }


def read_entries(transcript_path: str) -> list[dict]:
    from hooks.observability.transcript import complete_records

    entries, _, _ = complete_records(transcript_path)
    return _normalize(entries)


def _normalize(records: list[dict]) -> list[dict]:
    if any(record.get("type") in ("session_meta", "response_item", "turn_context") for record in records):
        from hooks.observability.codex_transcript import normalize_entries

        return normalize_entries(records)
    return records


def _cursor_path(session_id: str) -> Path:
    safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id)
    return CURSOR_DIR / f"{safe_id}.json"


def _cursor(session_id: str) -> dict:
    try:
        data = json.loads(_cursor_path(session_id).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _exported_turns(session_id: str) -> int:
    try:
        return int(_cursor(session_id)["turns"])
    except (ValueError, TypeError, KeyError):
        return 0


_TRUNCATED = re.compile(r"…\[truncated (\d+) chars\]\Z")


def _truncated_fields(spans: list[SpanSpec]) -> int:
    return sum(1 for spec in spans for key in spec.attributes if key.startswith("agentihooks.truncation."))


def _root_attributes(session_id: str) -> dict:
    from hooks.observability import correlation, signals

    accepted = _cursor(session_id).get("accepted_at")
    freshness = {
        "agentihooks.export.generated_at": datetime.now(timezone.utc).isoformat(),
        "agentihooks.export.queued.state": correlation.UNSUPPORTED,
        "agentihooks.export.trigger": os.environ.get(TRIGGER_ENV, "direct"),
        "agentihooks.export.unwritten_events.state": "unavailable",
    }
    if accepted:
        freshness["agentihooks.export.last_accepted_at"] = accepted
    else:
        freshness["agentihooks.export.last_accepted_at.state"] = correlation.MISSING
    return {**correlation.resolve(session_id), **signals.attributes(session_id), **freshness}


def _collector_outcomes(session_id: str, spans: list[SpanSpec], result: str, truncated: int) -> None:
    from hooks.observability import otel, signals

    keys = ("gen_ai.tool.name", "gen_ai.tool.call.id", "tool.outcome", "tool.outcome.state")
    for spec in spans:
        if spec.attributes.get("langfuse.observation.type") != "tool":
            continue
        fields = {key: spec.attributes[key] for key in keys if key in spec.attributes}
        otel.emit_event(
            "agentihooks.tool.outcome",
            {
                "session.id": session_id,
                **fields,
                "tool.started_at_ns": spec.start_ns,
                "tool.ended_at_ns": spec.end_ns,
                "agentihooks.export.result": result,
            },
        )
    signals.record({(session_id, "traces", result): len(spans), (session_id, "traces", "truncated"): truncated})
    otel.flush()


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


def _batches(spans: list[SpanSpec]):
    batch: list[SpanSpec] = []
    size = 0
    for spec in spans:
        weight = sum(len(value) for value in spec.attributes.values() if isinstance(value, str))
        if batch and size + weight > BATCH_CHARS:
            yield batch
            batch, size = [], 0
        batch.append(spec)
        size += weight
    yield batch


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
    from hooks.secrets import redact

    endpoint = (otel.langfuse_exporter_config() or {}).get("endpoint", "")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sys.stderr.write(
        f"{stamp} agent_trace export failed endpoint={endpoint} status={status} reason={redact(reason, mode='strict')}\n"
    )


def _save_progress(session_id: str, state: dict) -> None:
    import tempfile

    path = _cursor_path(session_id)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        json.dump(state, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        temporary.replace(path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def _progress(session_id: str) -> dict:
    path = _cursor_path(session_id)
    if not path.exists():
        return {"version": 2, "records": {}, "accepted": {}, "pending": [], "source": {}}
    state = json.loads(path.read_text())
    if state.get("version") == 2:
        return state
    if "version" in state:
        raise ValueError("unsupported exporter progress version")
    return {
        "version": 2,
        "records": {},
        "accepted": {},
        "pending": [],
        "source": {},
        "legacy_turns": state.get("turns", 0),
    }


def _record_revisions(records: dict) -> dict:
    return {
        key: hashlib.sha256(json.dumps(record, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        for key, record in records.items()
    }


def _waiting(state: dict, records: dict) -> dict:
    revisions = _record_revisions(records)
    accepted = state.get("accepted_records", {})
    return {key: record for key, record in records.items() if accepted.get(key) != revisions[key]}


def _pending_bytes(state: dict, records: dict, pending: list) -> int:
    return len(json.dumps({"records": _waiting(state, records), "pending": pending}, ensure_ascii=False).encode())


def _stage_source(state: dict, transcript_path: str) -> None:
    from hooks.observability.transcript import iter_complete_records, mask_value, record_id

    merged = dict(state["records"])
    legacy = "legacy_turns" in state and not merged
    point = _resume_point(state, transcript_path)
    position, count, unsupported = point["offset"], point["records"], point["unsupported"]
    size = _pending_bytes(state, merged, state["pending"])
    waiting = bool(_waiting(state, merged))
    empty = not state["pending"]
    state.pop("overflow", None)
    for record, end in iter_complete_records(transcript_path, position):
        if record is None:
            unsupported, position = unsupported + 1, end
            continue
        key = record_id(record)
        masked = mask_value(record)
        source_id = str(count) if legacy and record.get("type") == "response_item" else key
        masked["_source_id"] = merged.get(key, {}).get("_source_id", source_id)
        if merged.get(key) != masked:
            weight = len(json.dumps({key: masked}, ensure_ascii=False).encode()) - (0 if waiting else 2)
            if size + weight > PENDING_MAX_BYTES and not empty:
                state["overflow"] = {"bytes": size + weight, "limit": PENDING_MAX_BYTES}
                _report_progress(state, "overflow")
                break
            size, empty, waiting = size + weight, False, True
            merged[key] = masked
        position, count = end, count + 1
    state["records"] = merged
    state["unsupported_records"] = unsupported
    stat = Path(transcript_path).stat()
    state["source"] = {
        "device": stat.st_dev,
        "inode": stat.st_ino,
        "buffered_bytes": position,
        "records": count,
        "accepted_bytes": state["source"].get("accepted_bytes", 0),
    }


def _resume_point(state: dict, transcript_path: str) -> dict:
    """Where reading resumes: just past the last paged record when this source still holds it."""
    start = {"offset": 0, "records": 0, "unsupported": 0}
    paged = state.get("paged") or {}
    if not paged.get("boundary"):
        return start
    stat = Path(transcript_path).stat()
    point = paged.get("source") or {}
    if (point.get("device"), point.get("inode")) != (stat.st_dev, stat.st_ino) or not _still_at(transcript_path, point):
        point = {"device": stat.st_dev, "inode": stat.st_ino, **start}
    if point.get("boundary") != paged["boundary"]:
        point = _scan_to(transcript_path, point, paged["boundary"])
    paged["source"] = point
    return point


def _still_at(transcript_path: str, point: dict) -> bool:
    from hooks.observability.transcript import record_id

    if not point.get("offset"):
        return True
    with open(transcript_path, "rb") as handle:
        handle.seek(point["line"])
        line = handle.read(point["offset"] - point["line"])
    try:
        return line.endswith(b"\n") and record_id(json.loads(line)) == point["boundary"]
    except (ValueError, UnicodeDecodeError, AttributeError):
        return False


def _scan_to(transcript_path: str, point: dict, boundary: str) -> dict:
    from hooks.observability.transcript import iter_complete_records, record_id

    line, records, unsupported = point["offset"], point["records"], point["unsupported"]
    for record, end in iter_complete_records(transcript_path, point["offset"]):
        if record is None:
            unsupported += 1
        else:
            records += 1
            if record_id(record) == boundary:
                return {
                    **point,
                    "line": line,
                    "offset": end,
                    "records": records,
                    "unsupported": unsupported,
                    "boundary": boundary,
                }
        line = end
    return {**point, "boundary": boundary}


def _revision(spec: SpanSpec) -> str:
    data = asdict(spec)
    volatile = {
        "agentihooks.export.generated_at",
        "agentihooks.export.last_accepted_at",
        "agentihooks.export.last_accepted_at.state",
        "agentihooks.export.trigger",
    }
    data["attributes"] = {
        key: value
        for key, value in spec.attributes.items()
        if key not in volatile and not key.startswith("agentihooks.signals.traces.")
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _unsupported_io(records: list[dict], entries: list[dict]) -> int:
    dropped = sum(
        1
        for record in records
        if record.get("type") == "response_item"
        and record.get("payload", {}).get("type")
        not in ("message", "function_call", "custom_tool_call", "function_call_output", "custom_tool_call_output")
    )
    omitted = sum(
        1
        for record in records
        if record.get("type") == "response_item" and record.get("payload", {}).get("type") == "message"
        for block in record["payload"].get("content", [])
        if "text" not in block
    )
    normalized = sum(
        1
        for entry in entries
        for block in _blocks(entry)
        if not isinstance(block, dict) or block.get("type") not in ("text", "image", "tool_use", "tool_result")
    )
    return dropped + omitted + normalized


def _prepare_pending(session_id: str, state: dict, identity: Identity) -> None:
    from hooks.context.context_usage import session_cost
    from hooks.observability.transcript import mask_value

    paged = state.get("paged") or {}
    entries = _normalize(list(state["records"].values()))
    spans = session_spans(entries, identity, session_cost(session_id), root=_root_attributes(session_id), paged=paged)
    spans = list({spec.span_id: spec for spec in spans}.values())
    unsupported = _unsupported_io(list(state["records"].values()), entries) + paged.get("unsupported", 0)
    if spans:
        spans[0].attributes.update(
            {
                "agentihooks.export.spans": len(spans) + paged.get("spans", 0),
                "agentihooks.export.truncated_fields": _truncated_fields(spans) + paged.get("truncated", 0),
                "agentihooks.export.unsupported_io": unsupported,
                "agentihooks.export.unsupported_records": state.get("unsupported_records", 0),
                "agentihooks.export.replay_contract": "legacy-observations",
                "agentihooks.export.v4_replay.state": "unsupported",
            }
        )
    if "legacy_turns" in state:
        previous_entries = [entry for turn in turns(entries)[: state["legacy_turns"]] for entry in turn]
        for spec in session_spans(previous_entries, identity):
            state["accepted"].setdefault(f"{spec.span_id:016x}", "legacy")
    pending = []
    for spec in spans:
        spec.name = mask_value(spec.name)
        spec.attributes = mask_value(spec.attributes)
        key = f"{spec.span_id:016x}"
        if state["accepted"].get(key) != _revision(spec):
            pending.append(asdict(spec))
    state["pending"] = pending
    state["buffered_turns"] = len(turns(entries)) + paged.get("turns", 0)
    state["pending_source"] = dict(state["source"])
    state["pending_records"] = _record_revisions(state["records"])


def _report_progress(state: dict, result: str) -> None:
    from hooks.observability import signals

    session_id = state.get("session_id", "")
    signals.record({(session_id, "traces", result): 1})
    print(f"agent trace export {result}: {json.dumps(state.get('overflow', {}))}", file=sys.stderr)


def _batch_accepted(exporter, batch: list[SpanSpec], trace: int) -> bool:
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.trace.export import SpanExportResult

    readable = [_readable(spec, trace) for spec in batch]
    if not isinstance(exporter, OTLPSpanExporter):
        return exporter.export(readable) is SpanExportResult.SUCCESS
    import requests
    from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceResponse

    from hooks.observability import otel

    settings = otel.langfuse_exporter_config()
    response = requests.post(
        settings["endpoint"],
        data=encode_spans(readable).SerializeToString(),
        headers={"Content-Type": "application/x-protobuf", **settings["headers"]},
        timeout=otel.LANGFUSE_EXPORT_TIMEOUT_SEC,
    )
    if response.status_code != 200:
        _log_failure(response.status_code, response.reason)
        return False
    if not response.content:
        return True
    if "application/x-protobuf" in response.headers.get("Content-Type", ""):
        acknowledgement = ExportTraceServiceResponse.FromString(response.content)
        return acknowledgement.partial_success.rejected_spans == 0
    return _json_acknowledged(response.json())


def _json_acknowledged(acknowledgement: object) -> bool:
    if not isinstance(acknowledgement, dict) or "error" in acknowledgement or "errors" in acknowledgement:
        return False
    if not acknowledgement:
        return True
    if "partialSuccess" in acknowledgement or "partial_success" in acknowledgement:
        partial = acknowledgement.get("partialSuccess", acknowledgement.get("partial_success", {}))
        if not isinstance(partial, dict):
            return False
        rejected = partial.get("rejectedSpans", partial.get("rejected_spans", 0))
        return not isinstance(rejected, bool) and isinstance(rejected, (int, str)) and rejected in (0, "0")
    if "jobId" in acknowledgement:
        job_id = acknowledgement["jobId"]
        return isinstance(job_id, str) and bool(job_id)
    return all(
        isinstance(acknowledgement.get(key), str) and acknowledgement[key]
        for key in ("id", "name", "queueQualifiedName")
    ) and isinstance(acknowledgement.get("data"), dict)


def _send_pending(session_id: str, state: dict, exporter) -> None:
    trace = trace_id(session_id)
    specs = [SpanSpec(**record) for record in state["pending"]]
    for batch in _batches(specs):
        if not batch:
            continue
        if not _batch_accepted(exporter, batch, trace):
            _collector_outcomes(session_id, batch, "unconfirmed", _truncated_fields(batch))
            return
        new = [spec for spec in batch if f"{spec.span_id:016x}" not in state["accepted"]]
        for spec in batch:
            state["accepted"][f"{spec.span_id:016x}"] = _revision(spec)
        state["pending"] = state["pending"][len(batch) :]
        state["accepted_at"] = datetime.now(timezone.utc).isoformat()
        _save_progress(session_id, state)
        _collector_outcomes(session_id, new, "accepted", _truncated_fields(new))
        updated = [spec for spec in batch if spec not in new]
        if updated:
            _collector_outcomes(session_id, updated, "updated", _truncated_fields(updated))
    if not state["pending"] and state["accepted"]:
        state.setdefault("accepted_records", {}).update(state.get("pending_records", {}))
        accepted_source = state.get("pending_source", {})
        state["source"]["accepted_bytes"] = accepted_source.get("buffered_bytes", 0)
        state["turns"] = state.get("buffered_turns", 0)
        _page_accepted(session_id, state)
        _save_progress(session_id, state)


def _message_id(entry: dict) -> object:
    message = entry.get("message")
    return message.get("id") if isinstance(message, dict) else None


def _open_calls(entry: dict, results: dict) -> bool:
    return entry.get("type") == "assistant" and any(
        isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id") not in results
        for block in _blocks(entry)
    )


def _paging_cut(all_turns: list[list[dict]], results: dict) -> tuple[int, int] | None:
    """(turn, entry) of the first entry kept: everything before it is closed and may be paged out."""
    flat = [
        ((turn_index, entry_index), entry)
        for turn_index, turn in enumerate(all_turns)
        for entry_index, entry in enumerate(turn)
    ]
    cut = None
    for index, (position, entry) in enumerate(flat):
        message = _message_id(entry)
        if index and (message is None or message != _message_id(flat[index - 1][1])):
            cut = position
        if position[0] == len(all_turns) - 1 and _open_calls(entry, results):
            break
    return cut


def _aggregate(session_id: str, records: list[dict], paged: Mapping) -> dict:
    entries = _normalize(records)
    spans = session_spans(entries, Identity(session_id), paged=paged)
    root = spans[0]
    return {
        "turns": root.attributes["agent.turns"],
        "spans": {spec.span_id for spec in spans[1:]},
        "truncated": _truncated_fields(spans[1:]) + paged.get("truncated", 0),
        "unsupported": _unsupported_io(records, entries) + paged.get("unsupported", 0),
        "input_tokens": root.attributes["gen_ai.usage.input_tokens"],
        "output_tokens": root.attributes["gen_ai.usage.output_tokens"],
        "model": root.attributes["gen_ai.request.model"],
        "start_ns": root.start_ns,
    }


def _kept_records(values: list[dict], all_turns: list[list[dict]], cut: tuple[int, int]) -> list[int] | None:
    positions: dict = {}
    for index, record in enumerate(values):
        positions.setdefault(record.get("uuid"), index)
        positions.setdefault(f"codex-{record.get('_source_id')}", index)
    turn, entry = cut
    first = positions.get(all_turns[turn][entry].get("uuid"))
    prompt = positions.get(all_turns[turn][0].get("uuid"))
    if first is None or prompt is None:
        return None
    kept = set(range(first, len(values)))
    if entry:
        kept.add(prompt)
    contexts = [index for index in range(first) if values[index].get("type") == "turn_context"]
    kept.update(contexts[-1:])
    return sorted(kept)


def _page_accepted(session_id: str, state: dict) -> None:
    """Drop accepted records whose observations can no longer change, carrying what the root still needs."""
    records = state["records"]
    if _waiting(state, records):
        return
    keys, values = list(records), list(records.values())
    entries = _normalize(values)
    all_turns = turns(entries)
    cut = _paging_cut(all_turns, _results(entries))
    kept = None if cut is None else _kept_records(values, all_turns, cut)
    if kept is None:
        return
    dropped = [index for index in range(len(values)) if index not in kept]
    if not dropped:
        return
    paged = state.get("paged") or {}
    turn, entry = cut
    carry = {}
    if entry:
        text = _turn_output(all_turns[turn][1:entry], paged.get("open") if turn == 0 else None)
        carry["open"] = {"text": text, "previous_ns": _ns(all_turns[turn][entry - 1])}
    full = _aggregate(session_id, values, paged)
    rest = _aggregate(session_id, [values[index] for index in kept], carry)
    target = {
        key: full[key] - rest[key] for key in ("turns", "truncated", "unsupported", "input_tokens", "output_tokens")
    }
    target.update(
        carry,
        spans=paged.get("spans", 0) + len(full["spans"] - rest["spans"]),
        model=full["model"],
        input=paged.get("input", _prompt_text(all_turns[0])),
        start_ns=full["start_ns"],
        boundary=keys[dropped[-1]],
        source=paged.get("source"),
    )
    for span_id in full["spans"] - rest["spans"]:
        del state["accepted"][f"{span_id:016x}"]
    state["records"] = {keys[index]: values[index] for index in kept}
    for name in ("accepted_records", "pending_records"):
        state[name] = {key: value for key, value in state[name].items() if key in state["records"]}
    state["paged"] = target


def export_session(session_id: str, transcript_path: str, identity: Identity | None = None) -> None:
    from hooks.observability import otel

    exporter = otel.langfuse_exporter()
    if exporter is None or not session_id or not transcript_path:
        return
    path = _cursor_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        with os.fdopen(descriptor, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = _progress(session_id)
            state["session_id"] = session_id
            _stage_source(state, transcript_path)
            if not state["pending"]:
                _prepare_pending(session_id, state, identity or identity_from_env(session_id))
            _save_progress(session_id, state)
            _send_pending(session_id, state, exporter)
    except Exception as error:  # noqa: BLE001
        _log_failure(None, f"{type(error).__name__}: {error}")
    finally:
        exporter.shutdown()
