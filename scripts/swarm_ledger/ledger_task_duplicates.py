"""Screens task adds against the tasks the ledger already holds, before the locked apply takes the write lock."""

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
REFUSAL = (
    'task {new} repeats task {id} "{title}" ({state}, rank {rank}, phase {phase}): {fate}. '
    'If it is a different change, add it with --not-duplicate "<why it differs>"'
)
UNCHECKED_PREFIX = "the duplicate check did not run"
UNCHECKED_WARNING = UNCHECKED_PREFIX + " for task {new} because {why}, so it was added unchecked"
UNANSWERED = "the classifier did not answer"
SLOW = "it took longer than {budget:g} seconds"
FAILED = "it failed with {error}"


@dataclass(frozen=True)
class Screen:
    refused: dict = field(default_factory=dict)
    warnings: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Gate:
    screen: Screen
    inner: object

    def apply(self, doc: dict, op: dict, ctx: object, apply_op: object) -> bool:
        reason = self.screen.refused.get(op["id"])
        return self.inner.apply(doc, op, ctx, _refuse(reason) if reason else apply_op)


def screen(doc: dict, ops: list[dict]) -> Screen:
    adds = [op for op in ops if checked(op)]
    if not adds:
        return Screen()
    found, why = _find(doc, adds)
    refused, warnings = {}, {}
    for op, match in zip(adds, found):
        if match == UNCHECKED:
            warnings[op["id"]] = UNCHECKED_WARNING.format(new=op["task"], why=why)
        elif match is not None:
            refused[op["id"]] = refusal(op, match)
    return Screen(refused, warnings)


def checked(op: dict) -> bool:
    if op["op"] != "task_add" or op.get("not_duplicate"):
        return False
    return not (op["by"] == TICK and op["task"] in {f"{kind}-{op.get('phase', '')}" for kind in MINTED})


def refusal(op: dict, match: dict) -> str:
    fate = "it is already built" if match["state"] == "done" else "it is on the ledger and will be built"
    return REFUSAL.format(
        new=op["task"],
        id=match["id"],
        title=match["title"],
        state=match["state"],
        rank=match["rank"],
        phase=match["phase_title"] or "none",
        fate=fate,
    )


def find(doc: dict, adds: list[dict]) -> list:
    held = {key: doc.get(key, []) for key in ("tasks", "phases", "followups")}
    request = json.dumps({"doc": held, "kind": "task", "items": adds})
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


def _find(doc, adds):
    try:
        return find(doc, adds), UNANSWERED
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


def _refuse(reason):
    def refuse(doc, op, ctx):
        ctx.refused.append(reason)
        return False

    return refuse
