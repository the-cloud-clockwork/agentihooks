from typing import Any

from scripts.swarm import freeze


def guarded(doc: dict[str, Any], graph: dict[str, Any], fix: str) -> list[dict[str, Any]]:
    # ruleid: tick-claim-gate-without-admitted-queue
    return [t for t in doc["tasks"] if not freeze.claim_gate(t, doc, graph, fix)]


def held_ids(doc: dict[str, Any], graph: dict[str, Any], fix: str) -> set[str]:
    # ruleid: tick-claim-gate-without-admitted-queue
    return {t["id"] for t in doc["tasks"] if freeze.held(t, doc, graph, fix)}


def each_task(doc: dict[str, Any], graph: dict[str, Any], fix: str) -> list[str]:
    found = []
    for row in doc["tasks"]:
        # ruleid: tick-claim-gate-without-admitted-queue
        if freeze.claim_gate(row, doc, graph, fix):
            found.append(row["id"])
    return found


def queued(doc: dict[str, Any], graph: dict[str, Any], fix: str) -> list[dict[str, Any]]:
    queue = freeze.admitted(doc, graph)
    # ok: tick-claim-gate-without-admitted-queue
    return [t for t in doc["tasks"] if not freeze.claim_gate(t, doc, graph, fix, queue)]


def one_task(row: dict[str, Any], doc: dict[str, Any], graph: dict[str, Any], fix: str) -> bool:
    # ok: tick-claim-gate-without-admitted-queue
    return freeze.held(row, doc, graph, fix)
