from typing import Any


def phase_only_when_held(task: dict[str, Any]) -> dict[str, Any]:
    # ruleid: thin-enhancement-without-phase
    operation = {"op": "task_add", "task": f"enhance-{task['id']}", "lane": "plan", "enhances": task["id"]}
    return operation | ({"phase": task["phase"]} if task.get("phase") else {})


def phase_always_named(task: dict[str, Any], phase: str) -> dict[str, Any]:
    # ok: thin-enhancement-without-phase
    return {"op": "task_add", "task": f"enhance-{task['id']}", "lane": "plan", "phase": phase, "enhances": task["id"]}
