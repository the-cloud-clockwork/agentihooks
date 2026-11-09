from typing import Any


def rows_from_document(doc: dict[str, Any]) -> dict[str, Any]:
    # ruleid: tick-step-rows-from-doc-tasks
    return {t["id"]: t for t in doc["tasks"]}


def rows_from_optional_tasks(doc: dict[str, Any]) -> dict[str, Any]:
    # ruleid: tick-step-rows-from-doc-tasks
    return {task["id"]: task for task in doc.get("tasks") or []}


def rows_from_whole_read(ledger: Any, slug: str) -> dict[str, Any]:
    # ruleid: tick-step-rows-from-doc-tasks
    return {t["id"]: t for t in ledger.state(slug)["tasks"]}


def rows_from_view(view: Any) -> dict[str, Any]:
    # ok: tick-step-rows-from-doc-tasks
    return {t["id"]: t for t in view.tasks}


def one_task(ctx: Any, task_id: str) -> object:
    # ok: tick-step-rows-from-doc-tasks
    return ctx.view.read("tasks", task_id)
