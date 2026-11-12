"""Health finding rows: the ledger events behind each finding, the agent's last outcome, and which rows were required.

A row is required by doctrine when it is a task state change the engineer loop must record, and by the task when it
is the agent's own comment on its claimed task or that task's phase, which the task prompt asks for as work lands.
"""

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from scripts.swarm.health.findings import HELD, NOT_TRANSITIONS, Finding, _outcome, _task_id

ROW_LIMIT = 20
TEXT_LIMIT = 60
DOCTRINE = frozenset({"task claimed", "task pr", "task parked", "task unparked", "task blocked", "task done"})
LABELS = {"doctrine": "required by doctrine", "task": "required by the task"}


def attach(
    found: list[Finding], events: list[dict[str, Any]], tasks: dict[str, dict[str, Any]], agents: list[dict[str, Any]]
) -> list[Finding]:
    """Each finding with the ledger rows behind its subject, newest last, and its agent's last outcome."""
    seats = {agent["seat"]: agent["name"] for agent in agents if agent.get("seat")}
    return [_with_rows(finding, events, tasks, seats.get(finding.subject, finding.subject)) for finding in found]


def _with_rows(finding: Finding, events: list[dict[str, Any]], tasks: dict[str, dict[str, Any]], actor: str) -> Finding:
    if finding.subject in tasks:
        actor = tasks[finding.subject].get("claimed_by") or ""
        mine = [e for e in events if e.get("target") == f"tasks/{finding.subject}"]
    else:
        mine = [e for e in events if actor and e.get("by") == actor]
    if finding.kind == "ceremony":
        mine = [e for e in mine if e.get("kind") not in NOT_TRANSITIONS]
    mine = sorted(mine, key=lambda e: e.get("at") or 0)
    owned = _owned(tasks, actor)
    rows = tuple(_row(e, tasks, owned) for e in mine[-ROW_LIMIT:])
    return replace(finding, rows=rows, rows_total=len(mine), last_outcome=_last_outcome(events, tasks, actor))


def _owned(tasks: dict[str, dict[str, Any]], actor: str) -> set[tuple[str, str]]:
    held = [task for task in tasks.values() if actor and task.get("claimed_by") == actor and task.get("state") in HELD]
    targets = {f"tasks/{task['id']}" for task in held} | {
        f"phases/{task['phase']}" for task in held if task.get("phase")
    }
    return {(actor, target) for target in targets}


def _row(event: dict[str, Any], tasks: dict[str, dict[str, Any]], owned: set[tuple[str, str]]) -> dict[str, Any]:
    kind, target = event.get("kind", ""), event.get("target", "")
    required = ""
    if kind in DOCTRINE:
        required = "doctrine"
    elif kind == "comment added" and (event.get("by", ""), target) in owned:
        required = "task"
    label = " ".join(_label(event, tasks).split())
    return {
        "at": event.get("at") or 0,
        "kind": kind,
        "target": target,
        "text": label[:TEXT_LIMIT],
        "required": required,
    }


def _label(event: dict[str, Any], tasks: dict[str, dict[str, Any]]) -> str:
    if event.get("text"):
        return str(event["text"])
    if event.get("after"):
        return f"after {event['after']}"
    tid = _task_id(event.get("target") or "")
    return (tasks.get(tid, {}).get("title") or "") if tid else str(event.get("error") or "")


def _last_outcome(events: list[dict[str, Any]], tasks: dict[str, dict[str, Any]], actor: str) -> str:
    done = [
        e
        for e in events
        if actor
        and e.get("by") == actor
        and e.get("kind") == "task done"
        and _outcome(tasks.get(_task_id(e.get("target") or ""), {}))
    ]
    if not done:
        return "none recorded"
    last = max(done, key=lambda e: e.get("at") or 0)
    tid = _task_id(last["target"])
    return f"{tasks[tid].get('title') or tid} at {_stamp(last.get('at') or 0)}"


def _stamp(at_ms: int) -> str:
    return datetime.fromtimestamp(at_ms / 1000, UTC).strftime("%Y-%m-%d %H:%M")


def text(found: dict[str, Any]) -> str:
    """The status block for one finding: summary, evidence, threshold, id, last outcome and its rows."""
    lines = [f"finding  {found['kind']}  {found['subject']}: {found['summary']}"]
    lines += [f"  - {entry}" for entry in found["evidence"]]
    lines.append(f"  threshold {found['threshold']}")
    verdict = found.get("verdict")
    lines.append(f"  id {found['id']}" + (f"  earlier verdict {verdict['value']}" if verdict else ""))
    rows, total = found.get("rows", []), found.get("rows_total", 0)
    if "last_outcome" in found:
        lines.append(f"  last outcome {found['last_outcome']}")
        lines.append(_count(len(rows), total))
    for row in rows:
        cells = [_stamp(row["at"]), row["kind"], row["target"], row["text"], LABELS.get(row["required"], "")]
        lines.append("    " + "  ".join(cell for cell in cells if cell))
    return "\n".join(lines) + "\n"


def _count(shown: int, total: int) -> str:
    if not total:
        return "  rows none in the ledger events"
    return f"  rows latest {shown} of {total}" if total > shown else f"  rows {total}"
