from scripts.swarm_ledger import ledger_kinds


def ready(task: dict, rows: dict) -> bool:
    dependencies = task.get("depends_on") or []
    measured = [dep for dep in dependencies if measures(task, rows.get(dep, {}))]
    finish = (task.get("parked_on") or []) + (dependencies if task.get("pr_url") else measured)
    if any(rows.get(dep, {}).get("state") != "done" for dep in finish):
        return False
    return all(stackable(rows.get(dep, {})) for dep in dependencies)


def measures(task: dict, dependency: dict) -> bool:
    return task.get("kind") == "tune" and not ledger_kinds.read_only(dependency)


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
