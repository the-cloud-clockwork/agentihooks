"""agentihooks plan read: the task's plan chunk with ten lines of margin on each side.

agentihooks plan read [--task ID] [--phase ID] [--slug SLUG]

The task defaults to AGENTIHOOKS_SWARM_TASK and the ledger to AGENTIHOOKS_SWARM.
--phase prints the phase's whole plan range with the same margin.
"""

import argparse
import json
import os
import sys

from scripts.swarm_ledger import HERE

COMMAND = "agentihooks plan read"
MARGIN = 10


def _ledger():
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    from scripts.swarm_ledger import ledger_core, plan_ranges

    return ledger_core, plan_ranges


def chunk(text: str, lines: str) -> str:
    start, end = _ledger()[1].bounds(lines)
    rows = text.splitlines()
    return "".join(f"{row}\n" for row in rows[max(1, start - MARGIN) - 1 : min(len(rows), end + MARGIN)])


def pointer(task: dict) -> str:
    if not task.get("plan_lines"):
        return ""
    return (
        f"Plan: run {COMMAND} to read only your slice of the plan, lines {task['plan_lines']} "
        f"with ten lines of margin each side; add --phase {task.get('phase')} for the whole phase."
    )


def read(doc: dict, slug: str, task_id: str | None, phase_id: str | None) -> str:
    lines = None
    if not phase_id:
        task = next((t for t in doc.get("tasks", []) if t.get("id") == task_id), None)
        if task is None:
            raise ValueError(f"no task {task_id} in ledger {slug}")
        if not task.get("plan_lines"):
            raise ValueError(f"task {task_id} has no plan lines")
        lines, phase_id = task["plan_lines"], task.get("phase")
    phase = next((p for p in doc.get("phases", []) if p.get("id") == phase_id), None)
    if phase is None:
        raise ValueError(f"no phase {phase_id} in ledger {slug}")
    ref = phase.get("plan_ref")
    if not ref:
        raise ValueError(f"phase {phase_id} has no plan range")
    return chunk(_ledger()[1].stored_text(ref, doc), lines or ref["lines"])


def main(argv=None, environ=None) -> int:
    env = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(prog="agentihooks plan", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    reader = sub.add_parser("read", help="print the task's plan chunk with ten lines of margin on each side")
    reader.add_argument("--task", help="task id; defaults to AGENTIHOOKS_SWARM_TASK")
    reader.add_argument("--phase", help="print this phase's whole plan range instead")
    reader.add_argument("--slug", help="ledger slug; defaults to AGENTIHOOKS_SWARM")
    args = parser.parse_args(argv)
    slug = args.slug or env.get("AGENTIHOOKS_SWARM", "")
    if not slug:
        raise SystemExit("plan read needs a swarm: pass --slug or run it inside a swarm session")
    task_id = None if args.phase else args.task or env.get("AGENTIHOOKS_SWARM_TASK", "")
    if task_id == "":
        raise SystemExit("plan read needs a task: pass --task or run it inside a swarm task session")
    try:
        doc = json.loads(_ledger()[0].paths(slug)[1].read_text(encoding="utf-8"))
    except OSError:
        raise SystemExit(f"no ledger {slug}") from None
    try:
        sys.stdout.write(read(doc, slug, task_id, args.phase))
    except ValueError as error:
        raise SystemExit(str(error)) from None
    return 0
