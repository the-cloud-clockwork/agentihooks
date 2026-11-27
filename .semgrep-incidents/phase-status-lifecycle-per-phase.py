from typing import Any

from scripts.swarm.phase_state import _done, _lifecycle, _waits, label, lifecycle


def status(doc: dict[str, Any]) -> dict[str, dict[str, str]]:
    # ruleid: phase-status-lifecycle-per-phase
    return {p["id"]: {"state": lifecycle(p, doc), "label": label(p, doc)} for p in doc.get("phases", [])}


def status_by_loop(doc: dict[str, Any]) -> dict[str, str]:
    found = {}
    for phase in doc.get("phases", []):
        # ruleid: phase-status-lifecycle-per-phase
        found[phase["id"]] = lifecycle(phase, doc)
    return found


def status_once(doc: dict[str, Any]) -> dict[str, str]:
    phases = doc.get("phases", [])
    done, by_id = _done(phases), {p["id"]: p for p in phases}
    found = {}
    for phase in phases:
        # ok: phase-status-lifecycle-per-phase
        state = _lifecycle(phase, done, lambda: None)
        found[phase["id"]] = _waits(phase, by_id) if state == "waiting" else state
    return found


def outside_the_rule_scope(doc: dict[str, Any]) -> list[str]:
    # ok: phase-status-lifecycle-per-phase
    return [label(p, doc) for p in doc.get("phases", [])]
