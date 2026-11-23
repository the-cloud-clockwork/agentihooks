import re
from typing import Any

from scripts.swarm_ledger import ports

OPS = ("task_enhancement",)
OPERATOR = "operator"
VALUES = ("", "needed", "planned", "skipped")
WORKER_LANES = ("eng", "ci")
KEYS = {"op", "id", "by", "item", "enhancement"}
ITEM_RE = re.compile(r"^tasks/[^/]+$")
SHAPE = f"task_enhancement takes an id, an author by, an item tasks/<id> and an enhancement, one of {VALUES}"
UNKNOWN = "task {task} is not on this ledger"
SKIP = "{by} cannot skip the enhancement of task {task}: only the master or the operator skips it"
WORKER = "{by} works in the {lane} lane and cannot set the enhancement of task {task}"
PLANNED = (
    "{by} cannot mark the enhancement of task {task} planned: its closed enhancement plan task, the master or the "
    "operator does"
)


def check(op: dict[str, Any]) -> None:
    shaped = set(op) == KEYS and ITEM_RE.match(str(op["item"])) is not None
    if not (shaped and isinstance(op["by"], str) and op["by"] and op["enhancement"] in VALUES):
        raise ValueError(SHAPE)


def apply(doc: dict[str, Any], op: dict[str, Any], ctx: Any) -> bool:
    task_id = op["item"].split("/")[1]
    task = next((t for t in doc["tasks"] if t["id"] == task_id), None)
    refusal = UNKNOWN.format(task=task_id) if task is None else refused(doc, op, task_id)
    if task is None or refusal:
        ctx.refused.append(refusal)
        return False
    task["enhancement"] = op["enhancement"]
    ctx.stamp(f"{op['item']}/enhancement", op["by"])
    ctx.record(op["by"], "enhancement set", op["item"], text=op["enhancement"])
    return True


def refused(doc: dict[str, Any], op: dict[str, Any], task_id: str) -> str:
    by, value = op["by"], op["enhancement"]
    lane = ports.swarm().naming.lane_of(by)
    deciding = by == OPERATOR or lane == "master"
    if value == "skipped" and not deciding:
        return SKIP.format(by=by, task=task_id)
    if lane in WORKER_LANES:
        return WORKER.format(by=by, lane=lane, task=task_id)
    if value == "planned" and not (deciding or closed_by(doc, task_id, by)):
        return PLANNED.format(by=by, task=task_id)
    return ""


def closed_by(doc: dict[str, Any], task_id: str, by: str) -> bool:
    return any((t.get("enhances"), t.get("claimed_by"), t.get("state")) == (task_id, by, "done") for t in doc["tasks"])
