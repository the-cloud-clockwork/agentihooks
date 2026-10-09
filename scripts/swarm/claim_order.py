"""Claim order of ready tasks: rank, then a task resuming earlier work, then a small task that clears work fast,
then critical path depth.

Phase order is never used; ledger order breaks the remaining ties.
"""

from scripts.swarm_ledger import ledger_rank


def key(rows: dict):
    waiting = _waiting(rows)
    depth = depths(rows)

    def task_key(task):
        return (
            ledger_rank.order(task),
            not resumed(task),
            not _fast_clear(task, rows, waiting),
            -depth[task["id"]],
        )

    return task_key


def resumed(task: dict) -> bool:
    return bool(task.get("branch") or task.get("pr_url") or task.get("parked_on"))


def depths(rows: dict) -> dict[str, int]:
    waiting, found = _waiting(rows), {}
    for task_id in rows:
        _depth(task_id, waiting, found, frozenset())
    return {task_id: found[task_id] for task_id in rows}


def _depth(task_id, waiting, found, seen):
    if task_id not in found:
        seen = seen | {task_id}
        chains = [_depth(w, waiting, found, seen) + 1 for w in waiting.get(task_id, []) if w not in seen]
        found[task_id] = max(chains, default=0)
    return found[task_id]


def _waiting(rows):
    waiting = {}
    for task in rows.values():
        if _open(task):
            for dep in task.get("depends_on") or []:
                waiting.setdefault(dep, []).append(task["id"])
    return waiting


def _fast_clear(task, rows, waiting):
    return task.get("difficulty") == "S" and (_unblocks(task, rows, waiting) or _last_of_phase(task, rows))


def _unblocks(task, rows, waiting):
    return any(
        all(dep == task["id"] or rows.get(dep, {}).get("state") == "done" for dep in rows[w]["depends_on"])
        for w in waiting.get(task["id"], [])
    )


def _last_of_phase(task, rows):
    phase = task.get("phase")
    return bool(phase) and not any(
        other["id"] != task["id"] and other.get("phase") == phase and _open(other) for other in rows.values()
    )


def _open(task):
    return task.get("state") != "done"
