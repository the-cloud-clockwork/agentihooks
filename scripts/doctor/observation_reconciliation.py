from collections.abc import Callable

PAGE = 100
MAX_PAGES = 100


def _accepted(session_id, harness):
    from hooks.observability import agent_trace, trace_flush
    from hooks.observability.transcript import iter_complete_records
    from scripts.doctor.registry import load

    cursor = load(agent_trace._cursor_path(session_id))
    revisions = cursor.get("accepted") or {}
    ids = {key for key, revision in revisions.items() if revision != "legacy"}
    aggregate = len(ids) + int((cursor.get("paged") or {}).get("spans", 0))
    if not (cursor.get("paged") or {}).get("spans"):
        return ids, aggregate
    request = load(trace_flush.request_path(session_id))
    transcript = trace_flush._transcript(session_id, {"target": harness, **request})
    end = int((cursor.get("source") or {}).get("accepted_bytes", 0))
    if not transcript or not end:
        raise ValueError("accepted observation identities unavailable for paged cursor")
    records, position = [], 0
    for record, offset in iter_complete_records(transcript, 0):
        if offset > end:
            break
        if record is None:
            raise ValueError("accepted transcript contains an unreadable record")
        records.append(record)
        position = offset
    if position != end:
        raise ValueError("accepted transcript boundary is unavailable")
    spans = agent_trace.session_spans(agent_trace._normalize(records), agent_trace.Identity(session_id))
    ids.update(f"{span.span_id:016x}" for span in spans)
    ids.difference_update(key for key, revision in revisions.items() if revision == "legacy")
    return ids, aggregate


def _remote_ids(get, trace_id):
    ids = set()
    for page in range(1, MAX_PAGES + 1):
        body = get("observations", {"traceId": trace_id, "limit": PAGE, "page": page})
        ids.update(row["id"] for row in body["data"])
        total = body["meta"]["totalItems"]
        if page * PAGE >= total:
            if len(ids) != total:
                raise ValueError("observation identity listing is incomplete")
            return ids
    raise ValueError("observation identity page budget exhausted")


def reconcile(get: Callable[[str, dict], dict], session_id: str, harness: str, trace_id: str) -> dict | None:
    accepted, aggregate = _accepted(session_id, harness)
    if not aggregate:
        return None
    remote = _remote_ids(get, trace_id)
    return {"accepted": len(accepted), "missing": sorted(accepted - remote), "drift": aggregate - len(accepted)}
