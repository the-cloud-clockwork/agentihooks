def ready(task: dict, rows: dict) -> bool:
    if any(rows.get(dep, {}).get("state") != "done" for dep in task.get("parked_on") or []):
        return False
    return all(stackable(rows.get(dep, {})) for dep in task.get("depends_on") or [])


def stackable(task: dict) -> bool:
    return task.get("state") == "done" or (task.get("state") in ("claimed", "pr") and bool(task.get("branch")))


def held(task: dict, op: dict, tasks: list) -> bool:
    fields = op["fields"]
    claiming = (
        op["by"] == "swarm"
        and task.get("state", "open") == "open"
        and fields.get("state") in ("claimed", "pr")
        and bool(fields.get("claimed_by"))
    )
    return claiming and not ready(task, {row["id"]: row for row in tasks})
