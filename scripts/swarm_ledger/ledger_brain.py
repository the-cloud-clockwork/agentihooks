"""Ledger operations that record task learning outcomes, the brain policy and operator emergency bypasses."""

import hashlib
import json
import re
from typing import Any, Protocol, cast

OPS = ("brain_policy", "brain_learning", "brain_emergency")
SCOPE = frozenset({"purpose", "task", "revision", "generation", "brain"})
KINDS = ("lesson", "decision", "no_new_learning")
MAX_BYPASS_SECONDS = 300

Json = dict[str, Any]


class ApplyContext(Protocol):
    """The ledger sync context an extension operation writes through."""

    at: int
    meta: Json
    refused: list[str]
    dirty: bool

    def record(self, by: str, kind: str, target: str, **extra: object) -> None:
        """Append one ledger event."""
        ...


def mapping(value: object) -> Json | None:
    """Return the value as a JSON object, or None when it is not one."""
    return cast(Json, value) if isinstance(value, dict) else None


def text(value: object, limit: int) -> bool:
    """Return whether the value is a non blank string of at most limit characters."""
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit


def check_binding(value: object) -> None:
    """Refuse a binding that is not a complete task consultation scope."""
    binding = mapping(value)
    if (
        binding is None
        or frozenset(binding) != SCOPE
        or binding["purpose"] != "task"
        or not text(binding["task"], 200)
        or not text(binding["brain"], 200)
        or not isinstance(binding["revision"], str)
        or not re.fullmatch(r"[a-f0-9]{64}", binding["revision"])
        or type(binding["generation"]) is not int
        or binding["generation"] < 0
    ):
        raise ValueError("brain operation needs the current task binding")


def check_learning(value: object) -> None:
    """Refuse a learning outcome that is not a supported kind with bounded source evidence."""
    learning = mapping(value)
    if learning is None or set(learning) - {"seat"} != {"kind", "content", "sources"}:
        raise ValueError("learning needs kind, content and sources")
    if "seat" in learning and not text(learning["seat"], 200):
        raise ValueError("learning seat must be a bounded seat address")
    if learning["kind"] not in KINDS or not text(learning["content"], 4096):
        raise ValueError("learning needs a supported lesson, decision or explicit no new learning result")
    sources = cast(list[object], learning["sources"]) if isinstance(learning["sources"], list) else None
    if sources is None or len(sources) > 20 or not all(text(source, 1000) for source in sources):
        raise ValueError("learning sources must be bounded evidence references")
    if learning["kind"] != "no_new_learning" and not sources:
        raise ValueError("a lesson or decision needs source evidence")


def check(op: Json) -> None:
    """Refuse a brain operation whose fields do not match its contract."""
    common = {"op", "id", "item"}
    fields = {
        "brain_policy": {"mode", "brain"},
        "brain_learning": {"by", "binding", "learning", "proof"},
        "brain_emergency": {"binding", "expires", "reason"},
    }
    if op.get("op") not in fields or set(op) != common | fields[op["op"]]:
        raise ValueError("brain operation fields do not match the contract")
    if not isinstance(op["item"], str) or not re.fullmatch(r"tasks/[A-Za-z0-9._-]+", op["item"]):
        raise ValueError("brain operation needs a task")
    if op["op"] == "brain_policy":
        if op["mode"] not in ("observe", "required") or not text(op["brain"], 200):
            raise ValueError("brain policy needs a mode and brain")
        return
    check_binding(op["binding"])
    if op["op"] == "brain_learning":
        check_learning(op["learning"])
        if not text(op["by"], 64) or not isinstance(op["proof"], dict) or not op["proof"]:
            raise ValueError("learning needs an author and task proof")
    elif type(op["expires"]) not in (int, float) or not text(op["reason"], 1000):
        raise ValueError("emergency action needs expiry and reason")


def current(task: Json) -> Json:
    """Return the task's current consultation binding under its brain policy."""
    from scripts.gates.brain import bind

    return bind(task["id"], task, task.get("brain_policy", {}).get("brain", ""))


def bypassed(record: object, scope: Json, now: float) -> bool:
    """Return whether an emergency record covers this exact scope and has not expired."""
    held = mapping(record)
    return bool(held and held.get("binding") == {key: scope[key] for key in SCOPE} and held.get("expires", 0) > now)


def completion(task: Json) -> str | None:
    """Return why a task under a required brain policy cannot be done yet, or None when it can."""
    if task.get("brain_policy", {}).get("mode") != "required":
        return None
    scope = current(task)
    if any(
        record["binding"] == scope and record["proof"] == task.get("proof")
        for record in task.get("learning_outcomes", [])
    ):
        return None
    return "task needs a current learning outcome or explicit no new learning linked to its proof"


def refusal(op: Json, task: Json, at: float) -> str:
    """Return why the authority or scope of a brain operation is refused, or an empty string."""
    if op["op"] == "brain_policy":
        return "" if "by" not in op else "brain policy needs authenticated operator authority"
    if op.get("binding") != current(task):
        return "brain operation has a stale or unrelated task binding"
    if op["op"] == "brain_learning":
        if op["by"] != task.get("claimed_by") or op["proof"] != task.get("proof"):
            return "learning must belong to the current claimant and task proof"
    elif "by" in op or not at < op["expires"] <= at + MAX_BYPASS_SECONDS:
        return "emergency action needs authenticated operator authority and at most five minutes"
    return ""


def apply(doc: Json, op: Json, ctx: ApplyContext) -> bool:
    """Apply one brain operation to its task, recording audit, metrics and its idempotent identity."""
    task = next((row for row in doc.get("tasks", []) if f"tasks/{row['id']}" == op["item"]), None)
    if task is None:
        ctx.refused.append("brain operation task is absent")
        return False
    digest = hashlib.sha256(json.dumps(op, sort_keys=True).encode()).hexdigest()
    known = ctx.meta.get("brain_operations", {}).get(op["id"])
    if known is not None:
        if known == digest:
            return True
        ctx.refused.append("brain operation identity was reused with different input")
        return False
    if reason := refusal(op, task, ctx.at / 1000):
        ctx.refused.append(reason)
        return False
    record = {**op, "at": ctx.at, "phase": "accepted"}
    if op["op"] == "brain_policy":
        task["brain_policy"] = {"mode": op["mode"], "brain": op["brain"]}
    elif op["op"] == "brain_learning":
        task.setdefault("learning_outcomes", []).append(record)
        counts = ctx.meta.setdefault("brain_metrics", {}).setdefault("learning_outcomes_by_kind", {})
        kind = op["learning"]["kind"]
        counts[kind] = counts.get(kind, 0) + 1
    else:
        task["brain_bypass"] = record
        metrics = ctx.meta.setdefault("brain_metrics", {})
        metrics["brain_bypass_total"] = metrics.get("brain_bypass_total", 0) + 1
    ctx.meta.setdefault("brain_operations", {})[op["id"]] = digest
    ctx.meta.setdefault("brain_audit", []).append(record)
    ctx.record(op.get("by", "operator"), op["op"], op["item"], operation_id=op["id"])
    ctx.dirty = True
    return True
