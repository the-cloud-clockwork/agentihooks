from typing import Any


def phase_of(task: dict[str, Any], doc: dict[str, Any]) -> dict[str, Any]:
    phases = {phase["id"]: phase for phase in doc["phases"]}
    # ruleid: ledger-phase-lookup-unguarded
    return phases[task["phase"]]


def title_of(task: dict[str, Any], doc: dict[str, Any]) -> str:
    phases = {row["id"]: row for row in doc.get("phases", [])}
    # ruleid: ledger-phase-lookup-unguarded
    phase = phases[task["phase"]]
    return phase["title"]


def phase_or_none(task: dict[str, Any], doc: dict[str, Any]) -> dict[str, Any] | None:
    phases = {phase["id"]: phase for phase in doc["phases"]}
    # ok: ledger-phase-lookup-unguarded
    return phases.get(task["phase"])


def dependency_of(doc: dict[str, Any], phase_id: str) -> dict[str, Any]:
    phases = {phase["id"]: phase for phase in doc["phases"]}
    # ok: ledger-phase-lookup-unguarded
    return phases[phase_id]
