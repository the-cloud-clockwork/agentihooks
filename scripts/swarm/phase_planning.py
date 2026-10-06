"""The tick's planning pass: queue a planner for each auto phase that needs a plan, open the review of a finished slice, and add the release task of a release phase whose build tasks are done.

Only a running or drained swarm plans. Every write is keyed to facts in the ledger, so a second tick adds nothing.
"""

import os

from scripts.swarm import phase_state, slice_check
from scripts.swarm.ledger_events import SENDER, Mail
from scripts.swarm_ledger import ledger_comments

ACTIVE = ("running", "drained")
COMMENT_WORDS = ledger_comments.LIMITS["comment"]
TAIL_WORDS = 7
OPERATOR_REVIEWS = ("manual", "assist")
ASSIST_ASK = "post your recommendation as a comment on the phase, the operator approves"
MASTER_ASK = "approve it or send it back with a note"
RELEASE_CONTRACT = {
    "must": "The phase summary is a comment on the phase, and a changelog entry is merged into dev in every repo it touched.",
    "check": "Read the phase comments, and the changelog on dev in each repo the phase touched.",
    "judge": "the master",
}


def planning_pass(inbox, store, slug, doc, ledger, config):
    if config.state not in ACTIVE:
        return []
    mail, actions = Mail(inbox, store, slug), []
    for phase in doc["phases"]:
        stage = phase_state.lifecycle(phase, doc)
        if stage == "to_plan":
            actions += _queue(mail, slug, phase, ledger)
        elif stage == "in_review" and _unreviewed(phase):
            actions += _open_review(mail, slug, phase, doc, ledger, config)
        elif stage == "building" and _release_due(phase, doc):
            actions += _add_release(slug, phase, ledger)
    return actions


def _unreviewed(phase):
    review = phase.get("review") or {}
    return not review or review.get("state") == "sent_back" and not review.get("escalated")


def _queue(mail, slug, phase, ledger):
    pid, title = phase["id"], phase["title"]
    named = f"Slice phase {title} into tasks"
    task = {
        "task": f"plan-{pid}",
        "title": "Slice this phase into tasks" if ledger_comments.problems(named, "item") else named,
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
    rounds = (phase.get("review") or {}).get("rounds", 0)
    ledger.review_phase(slug, pid, "pending")
    if config.autonomy in OPERATOR_REVIEWS:
        ledger.priority(slug, f"phases/{pid}", "Approve the slice planned for this phase or send it back.")
    if config.autonomy != "manual":
        ask = ASSIST_ASK if config.autonomy == "assist" else MASTER_ASK
        found = " ".join(problems) or "The slice check found no problems."
        text = f"Review the slice planned for phase {pid} {phase['title']}: {ask}. {found}"
        mail.send(f"plan-review:{pid}:{rounds}", mail.master, text)
    size = len(slice_check.slice_ids(slice_check.plan_task(phase, doc)))
    ledger.comment_phase(slug, pid, _comment(problems, size), SENDER)
    return [f"opened the review of phase {pid}"]


def _release_due(phase, doc):
    if not phase.get("release"):
        return False
    rid = f"release-{phase['id']}"
    mine = [t for t in doc["tasks"] if t.get("phase") == phase["id"] and not t.get("out_of_scope")]
    return bool(mine) and all(t["id"] != rid and t.get("state") == "done" for t in mine)


def _add_release(slug, phase, ledger):
    pid, title = phase["id"], phase["title"]
    named = f"Release phase {title}"
    task = {
        "task": f"release-{pid}",
        "title": "Release this phase" if ledger_comments.problems(named, "item") else named,
        "lane": "eng",
        "kind": "ops",
        "phase": pid,
        "description": (
            f"Release phase {pid} {title}: post the phase summary as a comment on the phase, and merge a changelog "
            "entry into dev in every repo the phase touched. Version bumps and the release dance stay with the operator."
        ),
        "contract": RELEASE_CONTRACT,
    }
    ledger.add_task(slug, task, SENDER)
    return [f"added release-{pid} for phase {pid}"]


def _comment(problems, size):
    if not problems:
        noun = "task" if size == 1 else "tasks"
        return f"The slice check found no problems in the {size} {noun} of this slice."
    lines = ["The slice check found these problems."]
    for line in problems:
        fits = len(" ".join([*lines, line]).split()) <= COMMENT_WORDS - TAIL_WORDS
        if fits and not ledger_comments.problems(line, "comment"):
            lines.append(line)
    rest = len(problems) - len(lines) + 1
    return " ".join([*lines, f"And {rest} more in the review item."] if rest else lines)
