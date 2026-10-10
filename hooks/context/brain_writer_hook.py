"""Brain writer hook — scans session transcript for brain markers, routes them.

Called from on_stop() in hook_manager.py. Reads the full session transcript,
extracts assistant response text, parses HTML comment markers, then POSTs
each marker to brain-api (``{BRAIN_URL}/marker``). Markers that cannot be
POSTed (brain-api down, BRAIN_URL unset) buffer in the local outbox and are
retried over HTTP on the next run, so the buffer self-empties once brain-api
is reachable again.

Outbox format: ~/.agentihooks/brain-outbox/<timestamp>-<type>-<uuid>.json
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hooks.common import log

# ── Inline marker regex (same patterns as brain-tools/markers.py) ────

_BLOCK_RE = re.compile(
    r"<!--\s*@(\w+)((?:\s+\w+=[^\s>]+|\s+\w+=\"[^\"]*\")*)\s*-->"
    r"(.*?)"
    r"<!--\s*@/\1\s*-->",
    re.DOTALL,
)
_ATTR_RE = re.compile(r'(\w+)=(?:"([^"]*)"|(\S+))')
_WRITER_TYPES = frozenset({"lesson", "milestone", "signal", "decision"})


_RESERVED_ATTRS = frozenset({"ts"})


def _parse_attrs(attr_str: str) -> dict[str, str]:
    """Parse marker attributes, dropping reserved names.

    `ts` is reserved: brain-api backdates a marker into its original dated
    vault files from attrs.ts, and that trust belongs only to the hook's own
    buffered-at timestamp — never to text the model emitted (which can echo
    injected content).
    """
    attrs = {m.group(1): m.group(2) or m.group(3) for m in _ATTR_RE.finditer(attr_str)}
    for k in _RESERVED_ATTRS:
        attrs.pop(k, None)
    return attrs


def _find_markers(text: str) -> list[dict[str, Any]]:
    """Extract brain markers from raw text. Returns list of marker dicts."""
    return [marker for marker, _ in _markers_at(text)]


def _markers_at(text: str) -> list[tuple[dict[str, Any], int]]:
    results = []
    for m in _BLOCK_RE.finditer(text):
        mtype = m.group(1).lower()
        if mtype not in _WRITER_TYPES:
            continue
        attrs = _parse_attrs(m.group(2))
        content = m.group(3).strip()
        if content:
            results.append(({"type": mtype, "attrs": attrs, "content": content}, m.start()))
    return results


# ── Transcript parsing ───────────────────────────────────────────────


def _parse_transcript_for_markers(transcript_path: str, max_markers: int) -> list[dict]:
    """Extract markers from assistant text in a transcript (claude or codex)."""
    from bisect import bisect_right

    from hooks.memory.transcript_reader import iter_transcript_records

    all_text: list[str] = []
    starts: list[int] = []
    stamps: list[object] = []
    offset = 0
    for rec in iter_transcript_records(transcript_path):
        if rec.get("kind") in ("assistant_text", "turn_complete") and rec.get("text"):
            all_text.append(rec["text"])
            starts.append(offset)
            stamps.append((rec.get("raw") or {}).get("timestamp"))
            offset += len(rec["text"]) + 1

    if not all_text:
        return []

    markers = []
    for marker, start in _markers_at("\n".join(all_text))[:max_markers]:
        stamp = stamps[bisect_right(starts, start) - 1]
        if stamp:
            marker["at"] = stamp
        markers.append(marker)
    return markers


# ── Outbox write ─────────────────────────────────────────────────────


def _write_to_outbox(markers: list[dict], session_id: str, outbox_dir: str) -> int:
    """Write markers as individual JSON files to the outbox directory."""
    outbox = Path(outbox_dir)
    outbox.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    count = 0
    for marker in markers:
        ts = now.strftime("%Y%m%dT%H%M%S")
        uid = uuid.uuid4().hex[:8]
        filename = f"{ts}-{marker['type']}-{uid}.json"
        body, idem = _marker_request(marker, session_id)
        payload = {
            "type": marker["type"],
            "content": marker["content"],
            "attrs": body["attrs"],
            "idempotency_key": idem,
            "session_id": session_id,
            "agent_name": os.getenv("AGENTICORE_AGENT_NAME", os.getenv("USER", "unknown")),
            "project": os.getenv("CLAUDE_PROJECT_DIR", ""),
            "ts": now.isoformat(),
        }
        # Atomic write: a killed hook process mid-write leaves a truncated
        # JSON that the outbox drain must quarantine. temp + rename makes the
        # file appear fully-formed or not at all.
        tmp = outbox / f".{filename}.tmp"
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(outbox / filename)
        count += 1
    return count


# ── HTTP publish ─────────────────────────────────────────────────────


def _marker_request(marker: dict, session_id: str, cwd: str | None = None) -> tuple[dict, str]:
    """Build the /marker POST body + idempotency key for one marker.

    The key depends only on the session, the event-time scope and the marker,
    never on the current folder, so every Stop and every outbox replay of one
    marker send the same key.
    """
    from hooks.context.project_identity import resolve_project
    from hooks.context.project_sessions import SCOPE_FIELDS, lookup, marker_scope

    attrs = dict(marker.get("attrs") or {})
    folder = os.getenv("CLAUDE_PROJECT_DIR", str(Path.cwd())) if cwd is None else cwd
    scope = marker["scope"] if "scope" in marker else marker_scope(session_id, marker, replay=cwd is not None)
    if scope is not None:
        for name in SCOPE_FIELDS:
            attrs.pop(name, None)
        for name, value in scope.items():
            attrs.setdefault(name, value)
    elif "attribution" not in attrs:
        identity = lookup(session_id) or resolve_project(attrs.get("cwd") or folder, {} if cwd is not None else None)
        if identity:
            for name, value in identity.attributes().items():
                attrs.setdefault(name, value)
    attrs.setdefault("session_id", session_id)
    attrs.setdefault("source", attrs.get("source") or os.getenv("AGENTICORE_AGENT_NAME", "agent"))

    content = marker["content"][:4096]
    body = {
        "type": marker["type"],
        "content": content,
        "attrs": attrs,
    }
    return body, _marker_key(marker, session_id, scope or {}, content)


def _marker_key(marker: dict, session_id: str, scope: dict, content: str) -> str:
    from hooks.config import AGENTIHOOKS_HOME
    from hooks.context.brain_adapter import brain_id
    from scripts.swarm_v2 import keyspace

    recorded = marker.get("idempotency_key")
    if isinstance(recorded, str) and keyspace.MARKER_KEY.fullmatch(recorded):
        return recorded
    record = keyspace.installation(Path(AGENTIHOOKS_HOME))
    if not keyspace.current(marker.get("at"), record):
        return keyspace.legacy_marker_key(session_id, marker["type"], content)
    namespace = keyspace.Namespace(record.installation_id, brain_id(), str(scope.get("project_id", "")))
    return keyspace.marker_key(namespace, session_id, marker["type"], str(scope.get("task", "")), content)


def _publish_to_http(markers: list[dict], session_id: str) -> tuple[int, list[dict]]:
    """POST each marker to brain-api /marker.

    Returns (success_count, failed_markers). Failed markers buffer in the
    outbox so nothing is lost on transient HTTP failure.
    """
    from hooks._brain_http import brain_http_enabled, post

    if not brain_http_enabled():
        return 0, markers

    success = 0
    failed: list[dict] = []
    for marker in markers:
        body, idem = _marker_request(marker, session_id)
        response = post("/marker", body=body, idempotency_key=idem)
        if response and response.get("ok"):
            success += 1
        else:
            failed.append(marker)
    return success, failed


def _drain_outbox(outbox_dir: str) -> int:
    """Re-POST buffered markers; delete each file once brain-api accepts it.

    Also sweeps the outbox's ``-backlog`` sibling — the dir where a stuck pile
    was parked in the SSH-sync era — so orphaned markers self-deliver once
    brain-api is reachable, with no cron and no manual replay.

    A payload the server rejects outright (400/404/422) quarantines with a
    .bad suffix exactly like an unparseable file — one poison marker must not
    wedge every file sorted after it, forever, on every session. Any other
    refusal (server down, 5xx, stale credentials) stops the loop; those are
    caller/server conditions where retrying the rest is futile and deleting
    anything would destroy good markers. Each file's original ``ts`` rides in
    attrs so brain-api backdates the marker instead of stamping replay time.
    """
    from hooks._brain_http import brain_http_enabled, post

    if not brain_http_enabled():
        return 0
    outbox = Path(outbox_dir)
    backlog = outbox.with_name(outbox.name + "-backlog")
    # Backlog first: months older than the live outbox, and append-only vault
    # targets keep arrival order — oldest-first stays chronological.
    files = [f for d in (backlog, outbox) if d.is_dir() for f in sorted(d.glob("*.json"))]

    drained = 0
    for f in files:
        try:
            payload = json.loads(f.read_text())
            attrs = dict(payload.get("attrs") or {})
            if payload.get("ts"):
                attrs.setdefault("ts", payload["ts"])
            marker = {
                "type": payload["type"],
                "content": payload["content"],
                "attrs": attrs,
                "idempotency_key": payload.get("idempotency_key"),
            }
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            try:
                f.rename(f.with_name(f.name + ".bad"))
                log("brain_writer: quarantined unparseable outbox file", {"file": f.name})
            except OSError:
                pass  # vanished mid-quarantine — a concurrent drain got it
            continue

        body, idem = _marker_request(
            marker, payload.get("session_id") or "", payload.get("cwd") or payload.get("project", "")
        )
        response = post("/marker", body=body, idempotency_key=idem, surface_http_errors=True)
        status = (response or {}).get("__http_status__")
        if status in (400, 404, 422):
            # Server rejected THIS payload — permanent, retrying is futile.
            # 401/403/429 stay out of this bucket: those are caller-credential
            # or rate conditions, and quarantining on them would destroy the
            # whole queue over a stale token.
            try:
                f.rename(f.with_name(f.name + ".bad"))
                log(
                    "brain_writer: quarantined server-rejected outbox file",
                    {"file": f.name, "status": status},
                )
            except OSError:
                pass
            continue
        if not (response and response.get("ok")):
            break
        # The outbox may be shared (Stop + SubagentStop, concurrent sessions,
        # fleet AGENTIHOOKS_HOME on EFS). A concurrent drain that already
        # unlinked this file made the same idempotent POST — same outcome, and
        # a crash here would abort the caller before its own new markers ship.
        try:
            f.unlink()
        except FileNotFoundError:
            pass
        drained += 1
    return drained


# ── Main entry point ─────────────────────────────────────────────────


def write_markers(session_id: str, transcript_path: str, last_message: str = "") -> dict:
    """Scan transcript for brain markers, POST to brain-api, buffer failures.

    Args:
        last_message: The last assistant message from the Stop payload.
            Used as fallback when the JSONL transcript hasn't been flushed yet
            (race condition in -p mode).
    """
    from hooks.config import (
        BRAIN_WRITER_ENABLED,
        BRAIN_WRITER_MAX_MARKERS,
        BRAIN_WRITER_OUTBOX,
    )

    if not BRAIN_WRITER_ENABLED:
        return {"markers": 0, "reason": "disabled"}

    from hooks.telemetry import span_ctx

    with span_ctx(
        "brain.marker_write",
        {
            "session_id": session_id,
            "transcript_path": transcript_path or "<fallback>",
            "source": "transcript" if transcript_path else "last_message",
        },
    ) as span:
        # Retry markers buffered by earlier runs before touching new ones —
        # the outbox self-empties the moment brain-api is reachable again.
        drained = _drain_outbox(BRAIN_WRITER_OUTBOX)

        if transcript_path:
            from hooks.context.project_sessions import observe_transcript

            observe_transcript(session_id, transcript_path)
        markers = _parse_transcript_for_markers(transcript_path, BRAIN_WRITER_MAX_MARKERS)

        # Fallback: if transcript had no markers but last_message does, parse that
        if not markers and last_message:
            now = datetime.now(timezone.utc).isoformat()
            markers = [{**m, "at": now} for m in _find_markers(last_message)[:BRAIN_WRITER_MAX_MARKERS]]
        if not markers:
            span.set_attrs({"markers_found": 0, "outbox_drained": drained})
            return {"markers": 0, "drained": drained}

        from hooks.context.project_sessions import marker_scope, unattributed_session_events_total

        markers = [{**m, "scope": marker_scope(session_id, m)} for m in markers]
        scopes = [m["scope"] for m in markers if m["scope"] is not None]
        span.set_attrs({"unattributed_session_events_total": unattributed_session_events_total(scopes)})

        # HTTP is the only transport — any marker we fail to POST buffers in
        # the outbox for the retry-drain above.
        http_count, pending = _publish_to_http(markers, session_id)
        outbox_count = _write_to_outbox(pending, session_id, BRAIN_WRITER_OUTBOX) if pending else 0

        span.set_attrs(
            {
                "markers_found": len(markers),
                "http_count": http_count,
                "outbox_count": outbox_count,
                "outbox_drained": drained,
                "marker_types": ",".join(m["type"] for m in markers),
            }
        )
        return {
            "markers": len(markers),
            "http": http_count,
            "outbox": outbox_count,
            "drained": drained,
            "types": [m["type"] for m in markers],
        }
