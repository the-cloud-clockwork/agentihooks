"""The tick's planning pass: queue a planner for each auto phase that needs a plan, and open the review of a finished slice.

Only a running or drained swarm plans. Every write is keyed to facts in the ledger, so a second tick adds nothing.
"""

import os

from scripts.swarm import phase_state, slice_check
from scripts.swarm.ledger_events import SENDER, Mail

ACTIVE = ("running", "drained")
COMMENT_WORDS = 50
TAIL_WORDS = 7
OPERATOR_REVIEWS = ("manual", "assist")


def planning_pass(inbox, store, slug, doc, ledger, config):
    if config.state not in ACTIVE:
        return []
    mail, actions = Mail(inbox, store, slug), []
    for phase in doc.get("phases", []):
        stage = phase_state.lifecycle(phase, doc)
        if stage == "to_plan":
            actions += _queue(mail, slug, phase, ledger)
        elif stage == "in_review" and not phase.get("review"):
            actions += _open_review(mail, slug, phase, doc, ledger, config)
    return actions


def _queue(mail, slug, phase, ledger):
    pid, title = phase["id"], phase["title"]
    task = {
        "task": f"plan-{pid}",
        "title": f"Slice phase {title} into tasks",
        "lane": "plan",
        "kind": "plan",
        "phase": pid,
        "description": f"Slice phase {pid} {title} into tasks for review. Add tasks only in phase {pid}.",
    }
    ledger.add_task(slug, task, SENDER)
    text = f"For your information: phase {pid} {title} needs a plan, so the swarm queued plan-{pid} for a planner."
    mail.send(f"plan-queued:{pid}", mail.master, text, fyi=True)
    return [f"queued plan-{pid} for phase {pid}"]


def _open_review(mail, slug, phase, doc, ledger, config):
    pid = phase["id"]
    problems = slice_check.check(phase, doc, slice_check.Limits.from_env(os.environ))
    ledger.review_phase(slug, pid, "pending", 0)
    found = " ".join(problems) or "The slice check found no problems."
    if config.autonomy in OPERATOR_REVIEWS:
        ledger.priority(slug, f"phases/{pid}", f"Approve the slice planned for phase {pid} or send it back.")
    if config.autonomy != "manual":
        ask = (
            "post your recommendation as a comment on the phase, the operator approves"
            if config.autonomy == "assist"
            else "approve it or send it back with a note"
        )
        text = f"Review the slice planned for phase {pid} {phase['title']}: {ask}. {found}"
        mail.send(f"plan-review:{pid}", mail.master, text)
    ledger.comment_phase(
        slug, pid, _comment(problems, len(slice_check.slice_ids(slice_check.plan_task(phase, doc)))), SENDER
    )
    return [f"opened the review of phase {pid}"]


def _comment(problems, size):
    if not problems:
        noun = "task" if size == 1 else "tasks"
        return f"The slice check found no problems in the {size} {noun} of this slice."
    lines = [f"The slice check found {len(problems)} problems."]
    for shown, line in enumerate(problems):
        if len(" ".join([*lines, line]).split()) > COMMENT_WORDS - TAIL_WORDS:
            return " ".join([*lines, f"And {len(problems) - shown} more in the review item."])
        lines.append(line)
    return " ".join(lines)
