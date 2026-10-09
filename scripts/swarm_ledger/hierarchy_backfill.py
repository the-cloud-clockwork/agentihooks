import copy
import hashlib
import json
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from scripts.swarm_ledger import ledger_plans
from scripts.swarm_ledger.repository import hierarchy, repository
from scripts.swarm_ledger.repository import sqlite as store


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def conflict(rows: list, kind: str, item: str, before: object, after: object) -> None:
    rows.append({"kind": kind, "item": item, "before": before, "after": after})


def counts(doc: dict) -> dict:
    nodes, dependencies = hierarchy.project(doc)
    return {
        **{key: len(doc.get(key, [])) for key in ("plans", "phases", "slices", "tasks")},
        "nodes": len(nodes),
        "dependencies": len(dependencies),
        "phases_in_plans": sum(bool(row.get("plan")) for row in doc.get("phases", [])),
        "tasks_in_phases": sum(bool(row.get("phase")) for row in doc.get("tasks", [])),
        "tasks_in_slices": sum(bool(row.get("slice")) for row in doc.get("tasks", [])),
    }


def phase_plan(doc: dict, phase: dict, slug: str) -> dict:
    plans = doc["plans"]
    artifact = (phase.get("plan_ref") or {}).get("artifact", "")
    url = phase.get("plan_url", "")
    current = next((row for row in plans if f"plans/{row['id']}" == phase.get("plan")), None)
    if current is not None:
        return current
    identity = artifact or url
    existing = next(
        (row for row in plans if identity and row.get("artifact" if artifact else "url") == identity),
        None,
    )
    if existing is not None:
        return existing
    identifier = (
        ledger_plans.plan_id(PurePosixPath(urlsplit(artifact).path).stem)
        if artifact
        else ledger_plans.plan_id(digest(url))
        if url
        else f"standalone-{digest(slug)}"
    )
    if not identity and (standalone := next((row for row in plans if row["id"] == identifier), None)):
        return standalone
    plan = {"id": identifier, "title": phase["title"] if identity else "Standalone", "artifact": artifact, "url": url}
    if any(row["id"] == identifier for row in plans):
        plan["id"] = f"{identifier}-{digest(identity or slug)}"
    plans.append(plan)
    return plan


def assign_phases(doc: dict, slug: str, conflicts: list) -> None:
    for phase in doc.get("phases", []):
        plan = phase_plan(doc, phase, slug)
        address = f"plans/{plan['id']}"
        if phase.get("plan") and phase["plan"] != address:
            conflict(conflicts, "phase_plan", f"phases/{phase['id']}", phase["plan"], address)
        phase["plan"] = address
        links = [plan[key] for key in ("artifact", "url") if plan.get(key)]
        if phase.get("plan_url") and phase["plan_url"] not in links:
            conflict(conflicts, "phase_plan_link", f"phases/{phase['id']}", phase["plan_url"], plan["artifact"])
            phase["plan_url"] = plan["artifact"]


def task_slice(doc: dict, phase: dict, task: dict, conflicts: list) -> None:
    anchor = task.get("plan_slice")
    if not anchor:
        return
    identifier = ledger_plans.slice_id(phase["plan"].split("/")[1], anchor)
    address = f"phases/{phase['id']}"
    row = next((item for item in doc["slices"] if item["id"] == identifier), None)
    if row is not None and (row["phase"] != address or row.get("lines", "") != task.get("plan_lines", "")):
        conflict(conflicts, "slice_collision", f"tasks/{task['id']}", f"slices/{identifier}", address)
        identifier = f"{identifier}.{phase['id']}.{task['id']}"
        row = next((item for item in doc["slices"] if item["id"] == identifier), None)
    if row is None:
        row = {"id": identifier, "phase": address, "anchor": anchor, "lines": task.get("plan_lines", "")}
        doc["slices"].append(row)
    target = f"slices/{identifier}"
    if task.get("slice") and task["slice"] != target:
        conflict(conflicts, "task_slice", f"tasks/{task['id']}", task["slice"], target)
    task["slice"] = target


def assign_tasks(doc: dict, conflicts: list) -> None:
    phases = {row["id"]: row for row in doc.get("phases", [])}
    plans = {f"plans/{row['id']}": row for row in doc["plans"]}
    for task in doc.get("tasks", []):
        phase = phases.get(task.get("phase"))
        if phase is None:
            conflict(conflicts, "missing_phase", f"tasks/{task['id']}", task.get("phase"), None)
            continue
        plan = plans[phase["plan"]]
        links = [plan[key] for key in ("artifact", "url") if plan.get(key)]
        if task.get("plan_url") and task["plan_url"] not in links:
            target = phase.get("plan_url") or plan.get("artifact") or plan.get("url") or ""
            conflict(conflicts, "task_plan_link", f"tasks/{task['id']}", task["plan_url"], target)
            task["plan_url"] = target
        task_slice(doc, phase, task, conflicts)


def preview(doc: dict, slug: str) -> tuple[dict, dict]:
    after = copy.deepcopy(doc)
    after.setdefault("plans", [])
    after.setdefault("slices", [])
    conflicts = []
    assign_phases(after, slug, conflicts)
    assign_tasks(after, conflicts)
    report = {"applied": False, "before": counts(doc), "after": counts(after), "conflicts": conflicts}
    return after, report


def backfill(repo: store.SQLiteLedgerRepository, slug: str, by: str, apply: bool = False) -> dict:
    with store.read_only(repo.path.parent) as connection:
        if connection is None:
            raise store.Missing(slug)
        connection.execute("BEGIN")
        doc = repo._entry(connection, slug).state
        _, report = preview(doc, slug)
        report["drift"] = hierarchy.drift(hierarchy.stored(connection, slug), hierarchy.project(doc))
    return report


def run(args) -> None:
    from scripts.swarm_ledger import ledger_link

    if ledger_link.remote():
        raise SystemExit("hierarchy backfill must run on the local ledger host")
    print(json.dumps(backfill(repository, args.slug, args.name, args.apply), sort_keys=True))
