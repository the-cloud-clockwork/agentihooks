import copy
import hashlib
import json
import os
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlsplit

if TYPE_CHECKING:
    import argparse

from scripts.swarm_ledger import ledger_plans, storage
from scripts.swarm_ledger.repository import LedgerRepository, hierarchy
from scripts.swarm_ledger.repository.events import keep_events  # pyright: ignore[reportUnknownVariableType]

STANDALONE_DESCRIPTION = (
    "Tasks that belonged to no phase, moved here by the hierarchy backfill so every task sits under one plan."
)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def conflict(rows: list[dict[str, Any]], kind: str, item: str, before: object, after: object) -> None:
    rows.append({"kind": kind, "item": item, "before": before, "after": after})


def counts(doc: dict[str, Any]) -> dict[str, Any]:
    nodes, dependencies = cast(
        tuple[dict[str, Any], set[Any]],
        hierarchy.project(doc),  # pyright: ignore[reportUnknownMemberType]
    )
    phases = {row["id"] for row in doc.get("phases", [])}
    return {
        **{key: len(doc.get(key, [])) for key in ("plans", "phases", "slices", "tasks")},
        "nodes": len(nodes),
        "dependencies": len(dependencies),
        "phases_in_plans": sum(bool(row.get("plan")) for row in doc.get("phases", [])),
        "tasks_in_phases": sum(row.get("phase") in phases for row in doc.get("tasks", [])),
        "tasks_in_slices": sum(bool(row.get("slice")) for row in doc.get("tasks", [])),
    }


def phase_plan(
    doc: dict[str, Any], phase: dict[str, Any], slug: str, conflicts: list[dict[str, Any]]
) -> dict[str, Any]:
    plans = doc["plans"]
    artifact = cast(dict[str, Any], phase.get("plan_ref") or {}).get("artifact", "")
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
        ledger_plans.plan_id(PurePosixPath(urlsplit(cast(str, artifact)).path).stem)
        if artifact
        else ledger_plans.plan_id(digest(cast(str, url)))
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


def assign_phases(doc: dict[str, Any], slug: str, conflicts: list[dict[str, Any]]) -> None:
    ordered = sorted(
        doc["phases"], key=lambda row: not bool(cast(dict[str, Any], row.get("plan_ref") or {}).get("artifact"))
    )
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


def task_slice(
    doc: dict[str, Any], phase: dict[str, Any], task: dict[str, Any], conflicts: list[dict[str, Any]]
) -> None:
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


def assign_tasks(doc: dict[str, Any], conflicts: list[dict[str, Any]]) -> None:
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


def assign_unphased(doc: dict[str, Any], slug: str, conflicts: list[dict[str, Any]]) -> None:
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


def preview(doc: dict[str, Any], slug: str) -> tuple[dict[str, Any], dict[str, Any]]:
    after = copy.deepcopy(doc)
    after.setdefault("plans", [])
    after.setdefault("slices", [])
    conflicts: list[dict[str, Any]] = []
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
        ledger_plans.validate(after)  # pyright: ignore[reportUnknownMemberType]
    except ValueError as refused:
        report["refused"] = report["refused"] or str(refused)
    return after, report


def commit(repo: LedgerRepository, slug: str, by: str) -> dict[str, Any]:
    with repo.transaction() as transaction:
        entry = transaction.entry(slug)
        after, report = preview(cast(dict[str, Any], entry.state), slug)
        if report["refused"]:
            raise ValueError(json.dumps(report, sort_keys=True))
        report["repaired"] = cast(
            dict[str, Any],
            hierarchy.drift(transaction.hierarchy(slug), hierarchy.project(after)),  # pyright: ignore[reportUnknownMemberType]
        )
        if after != entry.state or report["repaired"]["drift"]:
            meta = after["_meta"]
            ctx = cast(Any, repo.domain).Context(meta, cast(Any, repo.domain).now_ms())
            ctx.record(by, "backfilled", "hierarchy")
            meta.update(rev=ctx.rev, updated_at=ctx.at)
            keep_events(meta, ctx.events, cast(Any, repo.domain), ctx.at)
            transaction.write(slug, entry, after, ctx.events)  # pyright: ignore[reportUnknownMemberType]
        report["applied"] = True
        report["drift"] = cast(
            dict[str, Any],
            hierarchy.drift(transaction.hierarchy(slug), hierarchy.project(after)),  # pyright: ignore[reportUnknownMemberType]
        )
    return report


def backfill(repo: LedgerRepository, slug: str, by: str, apply: bool = False) -> dict[str, Any]:
    if apply:
        return commit(repo, slug, by)
    with repo.snapshot() as snapshot:
        if snapshot is None:
            raise storage.Missing(slug)
        doc = cast(dict[str, Any], snapshot.document(slug))  # pyright: ignore[reportUnknownMemberType]
        _, report = preview(doc, slug)
        report["drift"] = cast(
            dict[str, Any],
            hierarchy.drift(snapshot.hierarchy(slug), hierarchy.project(doc)),  # pyright: ignore[reportUnknownMemberType]
        )
    return report


def run(args: "argparse.Namespace") -> None:
    from scripts.swarm_ledger import ledger_link

    if ledger_link.remote():  # pyright: ignore[reportUnknownMemberType]
        raise SystemExit("hierarchy backfill must run on the local ledger host")
    by = args.name or os.environ.get("AGENTIHOOKS_AGENT_NAME")
    if args.apply and not by:
        raise SystemExit("--as is required to apply the hierarchy backfill")
    print(json.dumps(backfill(storage.default(), args.slug, cast(str, by), args.apply), sort_keys=True))
