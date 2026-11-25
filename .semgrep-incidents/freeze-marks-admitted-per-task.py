from typing import Any

from scripts.swarm.freeze import admitted, unfocused


def marks_per_task(doc: dict[str, Any], graph: dict[str, Any], fix: str) -> list[str]:
    held = []
    for task in doc.get("tasks") or []:
        # ruleid: freeze-marks-admitted-per-task
        if unfocused(task, doc, graph, fix):
            held.append(task["id"])
    return held


def marks_by_keyword(doc: dict[str, Any], graph: dict[str, Any], fix: str) -> list[str]:
    # ruleid: freeze-marks-admitted-per-task
    return [task["id"] for task in doc.get("tasks") or [] if unfocused(task, doc, graph, fix_phase=fix)]


def marks_once(doc: dict[str, Any], graph: dict[str, Any], fix: str) -> list[str]:
    queue = admitted(doc, graph)
    held = []
    for task in doc.get("tasks") or []:
        # ok: freeze-marks-admitted-per-task
        if unfocused(task, doc, graph, fix, queue):
            held.append(task["id"])
    return held


def marks_once_by_keyword(doc: dict[str, Any], graph: dict[str, Any], fix: str) -> list[str]:
    queue = admitted(doc, graph)
    # ok: freeze-marks-admitted-per-task
    return [task["id"] for task in doc.get("tasks") or [] if unfocused(task, doc, graph, fix, admitted_queue=queue)]


def outside_the_rule_scope(doc: dict[str, Any], graph: dict[str, Any], fix: str) -> bool:
    # ok: freeze-marks-admitted-per-task
    return any(unfocused(task, doc, graph, fix) for task in doc.get("tasks") or [])
