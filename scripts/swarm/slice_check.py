"""The deterministic check of a planner's slice: one plain-words line per problem, never a refusal by itself."""

import re
from dataclasses import dataclass

from scripts.swarm.store import SwarmError
from scripts.swarm_ledger import ledger_kinds, plan_shape

MIN_WORDS = 20
DONE_WHEN_RE = re.compile(r"\bdone when\b", re.I)
CODE_KINDS = ("code", "ci")


@dataclass(frozen=True)
class Limits:
    max_tasks: int = 12
    max_areas: int = 6
    flag_confidence: float = 0.7

    @classmethod
    def from_env(cls, env):
        return cls(
            int(env.get("AGENTIHOOKS_PLAN_MAX_TASKS", 12)),
            int(env.get("AGENTIHOOKS_PLAN_MAX_AREAS", 6)),
            float(env.get("AGENTIHOOKS_PLAN_FLAG_CONFIDENCE", 0.7)),
        )


def slice_ids(plan):
    return [item.strip() for item in plan["proof"]["slice"].split(",") if item.strip()]


def plan_task(phase, doc):
    return next(t for t in doc["tasks"] if t.get("phase") == phase["id"] and ledger_kinds.kind(t) == "plan")


def check(phase, doc, limits):
    known = {t["id"]: t for t in doc["tasks"]}
    ids = slice_ids(plan_task(phase, doc))
    problems = _count(ids, limits)
    mine = []
    for tid in ids:
        task = known.get(tid)
        if task is None or task.get("phase") != phase["id"]:
            problems.append(f"Task {tid} is not in this phase.")
            continue
        mine.append(task)
        problems += _task(task, phase, doc, limits)
    return problems + _cycle(mine) + _unlined(phase, doc)


def _count(ids, limits):
    if not ids:
        return ["The slice holds no task."]
    if len(ids) > limits.max_tasks:
        return [f"The slice holds {len(ids)} tasks, more than {limits.max_tasks}."]
    return []


def _task(task, phase, doc, limits):
    tid, kind, description = task["id"], ledger_kinds.kind(task), task.get("description", "")
    problems = []
    if len(description.split()) < MIN_WORDS:
        problems.append(f"Task {tid} has a description under {MIN_WORDS} words.")
    elif not DONE_WHEN_RE.search(description):
        problems.append(f"Task {tid} has no done when sentence.")
    contract = task.get("contract") or {}
    if kind not in CODE_KINDS and not all(contract.get(key) for key in ledger_kinds.CONTRACT_KEYS):
        problems.append(f"Task {tid} is {kind} work without must, check and judge in its contract.")
    areas = task.get("territory") or []
    if kind in CODE_KINDS and not areas:
        problems.append(f"Task {tid} names no territory.")
    elif kind in CODE_KINDS and len(areas) > limits.max_areas:
        problems.append(f"Task {tid} names {len(areas)} areas, more than {limits.max_areas}.")
    problems += _dependencies(task, phase, doc)
    if task.get("lane") == "plan":
        problems.append(f"Task {tid} is in the plan lane.")
    return problems


def _dependencies(task, phase, doc):
    phases = {p["id"]: p for p in doc["phases"]}
    tasks = {t["id"]: t for t in doc["tasks"]}
    problems = []
    for dep in task.get("depends_on") or []:
        owner = phases.get(tasks.get(dep, {}).get("phase"))
        if owner is not None and owner["id"] != phase["id"] and not owner.get("done"):
            problems.append(f"Task {task['id']} depends on {dep} in a phase that is not done.")
    return problems


def _cycle(mine):
    ids = {t["id"] for t in mine}
    inside = [{**t, "depends_on": [d for d in t.get("depends_on") or [] if d in ids]} for t in mine]
    try:
        plan_shape.analyze(inside)
    except SwarmError:
        return ["The slice has a dependency cycle."]
    return []


def _unlined(phase, doc):
    return [
        f"Task {t['id']} links the plan but has no plan lines."
        for t in doc["tasks"]
        if t.get("phase") == phase["id"]
        and ledger_kinds.kind(t) != "plan"
        and t.get("plan_url")
        and not t.get("plan_lines")
    ]
