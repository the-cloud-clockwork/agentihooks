"""Screens task and follow up adds against what the ledger already holds, before the locked apply takes the write lock."""

import json
import os
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHILD = (sys.executable, "-m", "scripts.swarm_ledger.ledger_duplicates")
BUDGET_S = 5.0
UNCHECKED = "unchecked"
TICK = "swarm"
MINTED = ("plan", "release")
KINDS = {"task_add": "task", "add_item": "followup"}
LABELS = {"task": "task", "followup": "follow up"}
REFUSAL = (
    '{label} {new} repeats {kind} {id} "{title}" ({state}, rank {rank}, phase {phase}): {fate}. '
    'If it is a different change, add it with --not-duplicate "<why it differs>"'
)
UNCHECKED_PREFIX = "the duplicate check did not"
UNCHECKED_WARNING = UNCHECKED_PREFIX + " run for {label} {new} because {why}, so it was added unchecked"
LANDED_REPEAT = (
    '{label} {new} repeats {label} {id} "{title}", added while its duplicate check ran. '
    'If it is a different change, add it with --not-duplicate "<why it differs>"'
)
CONCURRENT = UNCHECKED_PREFIX + " compare {label} {new} with {ids}, added while it ran, so check them for repeats"
UNANSWERED = "the classifier did not answer"
SLOW = "it took longer than {budget:g} seconds"
FAILED = "it failed with {error}"


@dataclass(frozen=True)
class Screen:
    refused: dict = field(default_factory=dict)
    warnings: dict = field(default_factory=dict)
    seen: frozenset = frozenset()


@dataclass(frozen=True)
class Gate:
    screen: Screen
    inner: object

    def apply(self, doc: dict, op: dict, ctx: object, apply_op: object) -> bool:
        reason = self.screen.refused.get(op["id"]) or self.landed_repeat(doc, op)
        accepted = self.inner.apply(doc, op, ctx, _refuse(reason) if reason else apply_op)
        if accepted and op["id"] not in self.screen.warnings and checked(op):
            self.flag_concurrent(doc, op)
        return accepted

    def landed(self, doc: dict, op: dict) -> list[dict]:
        if op["op"] == "task_add":
            return [t for t in doc.get("tasks", []) if t["id"] not in self.screen.seen]
        return [f for f in doc.get("followups", []) if _seen(f) not in self.screen.seen and not f.get("done")]

    def landed_repeat(self, doc: dict, op: dict) -> str:
        if not checked(op):
            return ""
        title = _plain(_text(op))
        match = next((i for i in self.landed(doc, op) if _plain(_text(i)) == title), None)
        if not match:
            return ""
        return LANDED_REPEAT.format(label=LABELS[KINDS[op["op"]]], new=_new(op), id=match["id"], title=_text(match))

    def flag_concurrent(self, doc: dict, op: dict) -> None:
        if landed := [i["id"] for i in self.landed(doc, op)]:
            label = LABELS[KINDS[op["op"]]]
            self.screen.warnings[op["id"]] = CONCURRENT.format(label=label, new=_new(op), ids=", ".join(landed))


def screen(doc: dict, ops: list[dict]) -> Screen:
    adds = [op for op in ops if checked(op)]
    if not adds:
        return Screen()
    refused, warnings = {}, {}
    for kind in ("task", "followup"):
        if group := [op for op in adds if KINDS[op["op"]] == kind]:
            _judge(doc, kind, group, refused, warnings)
    seen = {t["id"] for t in doc.get("tasks", [])} | {op["task"] for op in ops if op["op"] == "task_add"}
    seen |= {_seen(f) for f in doc.get("followups", [])} | {_seen(op) for op in adds if op["op"] == "add_item"}
    return Screen(refused, warnings, frozenset(seen))


def checked(op: dict) -> bool:
    if op["op"] == "add_item":
        return op["list"] == "followups" and not op.get("not_duplicate")
    if op["op"] != "task_add" or op.get("not_duplicate"):
        return False
    return not (op["by"] == TICK and op["task"] in {f"{kind}-{op.get('phase', '')}" for kind in MINTED})


def refusal(op: dict, match: dict) -> str:
    fate = "it is already built" if match["state"] == "done" else "it is on the ledger and will be built"
    return REFUSAL.format(
        label=LABELS[KINDS[op["op"]]],
        new=_new(op),
        kind=LABELS[match["kind"]],
        id=match["id"],
        title=match["title"],
        state=match["state"],
        rank=match["rank"] or "none",
        phase=match["phase_title"] or "none",
        fate=fate,
    )


def find(doc: dict, adds: list[dict], kind: str = "task") -> list:
    held = {key: doc.get(key, []) for key in ("tasks", "phases", "followups")}
    request = json.dumps({"doc": held, "kind": kind, "items": adds})
    pipes = {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE}
    with subprocess.Popen(CHILD, **pipes, text=True, cwd=ROOT, start_new_session=True) as child:
        try:
            out, err = child.communicate(request, timeout=BUDGET_S)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            raise
    if child.returncode:
        raise Died(_error(err))
    return json.loads(out.splitlines()[-1])


class Died(Exception):
    pass


def _judge(doc, kind, adds, refused, warnings):
    found, why = _find(doc, adds, kind)
    for op, match in zip(adds, found):
        if match == UNCHECKED:
            warnings[op["id"]] = UNCHECKED_WARNING.format(label=LABELS[kind], new=_new(op), why=why)
        elif match is not None:
            refused[op["id"]] = refusal(op, match)


def _new(op):
    return op["task"] if op["op"] == "task_add" else op["id"]


def _seen(followup):
    return f"followups/{followup['id']}"


def _text(item):
    return item.get("title") or item["text"]


def _find(doc, adds, kind):
    try:
        return find(doc, adds, kind), UNANSWERED
    except subprocess.TimeoutExpired:
        why = SLOW.format(budget=BUDGET_S)
    except Died as exc:
        why = FAILED.format(error=exc)
    except Exception as exc:
        why = FAILED.format(error=type(exc).__name__)
    return [UNCHECKED] * len(adds), why


def _error(stderr):
    lines = stderr.strip().splitlines()
    return lines[-1].partition(":")[0] if lines else "no message"


def _plain(title):
    return title.casefold().split()


def _refuse(reason):
    def refuse(doc, op, ctx):
        ctx.refused.append(reason)
        return False

    return refuse
