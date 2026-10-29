from collections import Counter

KEPT = 10
COLLECTIONS = ("phases", "tasks", "questions", "followups", "alerts")
GROUPS = {"phases": "plan", "tasks": "slice"}


def closed(name: str, item: object) -> bool:
    if not isinstance(item, dict) or item.get("out_of_scope"):
        return False
    if name == "questions":
        return any(isinstance(a, dict) and not a.get("deleted") for a in item.get("answers") or [])
    if name == "alerts":
        return item.get("state") == "done"
    return bool(item.get("done")) or item.get("state") == "done"


def recent(name: str, rows: list) -> list:
    return [row for row in reversed(rows) if closed(name, row)]


def trimmed(state: dict) -> tuple[dict, dict]:
    view, hidden = {}, {}
    for name in COLLECTIONS:
        rows = state.get(name)
        if not isinstance(rows, list):
            continue
        done = [index for index, row in enumerate(rows) if closed(name, row)]
        drop = set(done[: len(done) - KEPT])
        if not drop:
            continue
        view[name] = [row for index, row in enumerate(rows) if index not in drop]
        hidden[name] = entry(name, [rows[index] for index in sorted(drop)], plan_keys(state))
    if "tasks" in hidden:
        hidden["tasks"]["plan_phases"] = plan_phases(state, hidden["tasks"]["ids"])
    return view, hidden


def plan_keys(state: dict) -> set:
    return {f"plans/{plan.get('id')}" for plan in state.get("plans") or [] if isinstance(plan, dict)}


def entry(name: str, gone: list, plans: set) -> dict:
    result = {"hidden": len(gone), "kept": KEPT, "ids": [row.get("id") for row in gone]}
    if name in GROUPS:
        keys = [
            (row.get("plan") if row.get("plan") in plans else "unplanned") if name == "phases" else row.get("slice")
            for row in gone
        ]
        result["groups"] = dict(Counter(key for key in keys if key))
    if name == "tasks":
        result["claimed_by"] = {row.get("id"): row["claimed_by"] for row in gone if row.get("claimed_by")}
    return result


def plan_phases(state: dict, ids: list) -> list:
    gone = set(ids)
    return sorted(
        {
            task.get("phase")
            for task in state.get("tasks") or []
            if isinstance(task, dict) and task.get("kind") == "plan" and task.get("id") in gone and task.get("phase")
        }
    )


def hidden_claims(state: dict) -> list:
    claims = state.get("_meta", {}).get("closed", {}).get("tasks", {}).get("claimed_by", {})
    return [{"id": task, "claimed_by": claimer} for task, claimer in claims.items()]
