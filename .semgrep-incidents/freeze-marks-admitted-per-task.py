from typing import Any

from scripts.swarm.freeze import admitted, unfocused


class PerTask:
    def marks(self, doc: dict[str, Any], graph: dict[str, Any], fix: str) -> list[str]:
        held = []
        for task in doc.get("tasks") or []:
            # ruleid: freeze-marks-admitted-per-task
            if unfocused(task, doc, graph, fix):
                held.append(task["id"])
        return held


class Once:
    def marks(self, doc: dict[str, Any], graph: dict[str, Any], fix: str) -> list[str]:
        queue = admitted(doc, graph)
        held = []
        for task in doc.get("tasks") or []:
            # ok: freeze-marks-admitted-per-task
            if unfocused(task, doc, graph, fix, queue):
                held.append(task["id"])
        return held


def notice(doc: dict[str, Any], graph: dict[str, Any], fix: str) -> bool:
    # ok: freeze-marks-admitted-per-task
    return any(unfocused(task, doc, graph, fix) for task in doc.get("tasks") or [])
