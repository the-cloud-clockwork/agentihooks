from typing import Any


def unguarded(task: dict[str, Any]) -> dict[str, Any]:
    # ruleid: thin-enhancement-without-expected
    return {"op": "task_enhancement", "item": f"tasks/{task['id']}", "enhancement": "needed"}


def guarded(task: dict[str, Any]) -> dict[str, Any]:
    # ok: thin-enhancement-without-expected
    return {
        "op": "task_enhancement",
        "item": f"tasks/{task['id']}",
        "enhancement": "needed",
        "expected": task.get("enhancement"),
    }
