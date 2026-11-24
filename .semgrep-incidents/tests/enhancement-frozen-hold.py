from typing import Any

from scripts.swarm.freeze import _enhancement_frozen, frozen, outside_watched, unfocused


def old_claim_gate(task: dict[str, Any], doc: dict[str, Any], graph: dict[str, Any], fix_phase: str) -> bool:
    records = doc.get("freezes") or []
    chain = [f"tasks/{task['id']}"]
    # ruleid: enhancement-frozen-hold
    return (
        frozen(task, records, chain) or unfocused(task, doc, graph, fix_phase) or outside_watched(task, doc, fix_phase)
    )


def guarded_claim_gate(task: dict[str, Any], doc: dict[str, Any], graph: dict[str, Any], fix_phase: str) -> bool:
    records = doc.get("freezes") or []
    chain = [f"tasks/{task['id']}"]
    # ok: enhancement-frozen-hold
    return (
        frozen(task, records, chain)
        or _enhancement_frozen(task, doc, graph)
        or unfocused(task, doc, graph, fix_phase)
        or outside_watched(task, doc, fix_phase)
    )
