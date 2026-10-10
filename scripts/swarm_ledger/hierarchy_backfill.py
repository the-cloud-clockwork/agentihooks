import copy
import hashlib
import json
import os
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from scripts.swarm_ledger import ledger_plans
from scripts.swarm_ledger.repository import hierarchy, repository
from scripts.swarm_ledger.repository import sqlite as store


STANDALONE_DESCRIPTION = (
    "Tasks that belonged to no phase, moved here by the hierarchy backfill so every task sits under one plan."
)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def conflict(rows: list, kind: str, item: str, before: object, after: object) -> None:
    rows.append({"kind": kind, "item": item, "before": before, "after": after})


def counts(doc: dict) -> dict:
    nodes, dependencies = hierarchy.project(doc)
    phases = {row["id"] for row in doc.get("phases", [])}
    return {
        **{key: len(doc.get(key, [])) for key in ("plans", "phases", "slices", "tasks")},
        "nodes": len(nodes),
        "dependencies": len(dependencies),
        "phases_in_plans": sum(bool(row.get("plan")) for row in doc.get("phases", [])),
        "tasks_in_phases": sum(row.get("phase") in phases for row in doc.get("tasks", [])),
        "tasks_in_slices": sum(bool(row.get("slice")) for row in doc.get("tasks", [])),
    }


def phase_plan(doc: dict, phase: dict, slug: str, conflicts: list) -> dict:
    plans = doc["plans"]
    artifact = (phase.get("plan_ref") or {}).get("artifact", "")
    url = phase.get("plan_url", "")
    current = next((row for row in plans if f"plans/{row['id']}" == phase.get("plan")), None)
    if current is not None and (
        not (artifact or url) or current.get("artifact" if artifact else "url") == (artifact or url)
    ):
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
        plan["id"] = f"{identifier}-{digest(identity)}"
        conflict(conflicts, "plan_collision", f"phases/{phase['id']}", f"plans/{identifier}", f"plans/{plan['id']}")
    plans.append(plan)
    return plan


def assign_phases(doc: dict, slug: str, conflicts: list) -> None:
    ordered = sorted(doc["phases"], key=lambda row: not bool((row.get("plan_ref") or {}).get("artifact")))
    for phase in ordered:
        plan = phase_plan(doc, phase, slug, conflicts)
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
        if task.get("slice"):
            row = next((item for item in doc["slices"] if f"slices/{item['id']}" == task["slice"]), None)
            if row is None or row["phase"] != f"phases/{phase['id']}":
                conflict(conflicts, "task_slice", f"tasks/{task['id']}", task["slice"], "")
                task.pop("slice")
        return
    current = next((item for item in doc["slices"] if f"slices/{item['id']}" == task.get("slice")), None)
    if (
        current
        and current["phase"] == f"phases/{phase['id']}"
        and current.get("anchor") == anchor
        and current.get("lines", "") == task.get("plan_lines", "")
    ):
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
    phases = {row["id"]: row for row in doc["phases"]}
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
        task_slice(doc, phase, task, conflicts)


def assign_unphased(doc: dict, slug: str, conflicts: list) -> None:
    identifier = f"standalone-{digest(slug)}"
    phases = doc.setdefault("phases", [])
    for task in doc.get("tasks", []):
        if task.get("phase"):
            continue
        if not any(row["id"] == identifier for row in phases):
            phases.append(
                {
                    "id": identifier,
                    "title": "Standalone",
                    "description": STANDALONE_DESCRIPTION,
                    "comments": [],
                    "done": False,
                }
            )
        conflict(conflicts, "missing_phase", f"tasks/{task['id']}", task.get("phase"), f"phases/{identifier}")
        task["phase"] = identifier


def preview(doc: dict, slug: str) -> tuple[dict, dict]:
    after = copy.deepcopy(doc)
    after.setdefault("plans", [])
    after.setdefault("slices", [])
    conflicts = []
    assign_unphased(after, slug, conflicts)
    assign_phases(after, slug, conflicts)
    assign_tasks(after, conflicts)
    dangling = [
        f"{row['item']} names phases/{row['before']}, which does not exist"
        for row in conflicts
        if row["kind"] == "missing_phase" and row["after"] is None
    ]
    report = {
        "applied": False,
        "before": counts(doc),
        "after": counts(after),
        "conflicts": conflicts,
        "refused": "; ".join(dangling),
    }
    try:
        ledger_plans.validate(after)
    except ValueError as refused:
        report["refused"] = report["refused"] or str(refused)
    return after, report


def commit(repo: store.SQLiteLedgerRepository, slug: str, by: str) -> dict:
    with repo.domain.LOCK, repo.connect() as connection, connection:
        connection.execute(store.BEGIN_IMMEDIATE)
        entry = repo._entry(connection, slug)
        after, report = preview(entry.state, slug)
        if report["refused"]:
            raise ValueError(json.dumps(report, sort_keys=True))
        report["repaired"] = hierarchy.drift(hierarchy.stored(connection, slug), hierarchy.project(after))
        if after != entry.state or report["repaired"]["drift"]:
            meta = after["_meta"]
            ctx = repo.domain.Context(meta, repo.domain.now_ms())
            ctx.record(by, "backfilled", "hierarchy")
            meta.update(rev=ctx.rev, updated_at=ctx.at)
            meta["events"] = (meta["events"] + ctx.events)[-repo.domain.EVENTS_KEPT :]
            written = repo._write(connection, slug, entry, after, ctx.events)
        else:
            written = entry
        report["applied"] = True
        report["drift"] = hierarchy.drift(hierarchy.stored(connection, slug), hierarchy.project(after))
    repo._remember(slug, written)
    return report


def stored(connection, slug: str) -> tuple[dict, set]:
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    nodes = (
        {
            node: (kind, parent, position)
            for node, kind, parent, position in connection.execute(hierarchy.NODES, (slug,))
        }
        if "work_nodes" in tables
        else {}
    )
    dependencies = set(connection.execute(hierarchy.DEPENDENCIES, (slug,))) if "work_dependencies" in tables else set()
    return nodes, dependencies


def backfill(repo: store.SQLiteLedgerRepository, slug: str, by: str, apply: bool = False) -> dict:
    if apply:
        return commit(repo, slug, by)
    with store.read_only(repo.path.parent) as connection:
        if connection is None:
            raise store.Missing(slug)
        connection.execute("BEGIN")
        rows = store.read_rows(connection, slug)
        if not rows:
            raise store.Missing(slug)
        doc = store.assemble(rows)
        _, report = preview(doc, slug)
        report["drift"] = hierarchy.drift(stored(connection, slug), hierarchy.project(doc))
    return report


def run(args) -> None:
    from scripts.swarm_ledger import ledger_link

    if ledger_link.remote():
        raise SystemExit("hierarchy backfill must run on the local ledger host")
    by = args.name or os.environ.get("AGENTIHOOKS_AGENT_NAME")
    if args.apply and not by:
        raise SystemExit("--as is required to apply the hierarchy backfill")
    print(json.dumps(backfill(repository, args.slug, by, args.apply), sort_keys=True))
