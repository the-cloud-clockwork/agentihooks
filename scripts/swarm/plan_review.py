"""Plan approve and send back: who decides a phase's slice at each autonomy level.

The ledger op records the decision and derives rounds and escalation; this module holds the autonomy rule and refuses an approval
the slice check faults unless an override names its reason.
"""

import os

from scripts.swarm import phase_state, slice_check
from scripts.swarm.store import ASSIST, MANUAL, MASTER, SwarmError

BUTTONS = "use Approve plan or Send back on the phase in the ledger page"
DECISIONS = {"approve": "approved", "send-back": "sent_back"}
RECOMMEND = "post your recommendation as a comment on the phase"


def refusal(agent, autonomy, phase, doc):
    stage = phase_state.lifecycle(phase, doc)
    if stage != "in_review":
        return f"phase {phase['id']} is not in review; it is {stage.replace('_', ' ')}"
    review = phase.get("review") or {}
    if review.get("escalated"):
        return f"the plan for phase {phase['id']} was sent back {review['rounds']} times, so the operator decides: {BUTTONS}"
    if autonomy in (MANUAL, ASSIST):
        advice = f"; {RECOMMEND}" if autonomy == ASSIST else ""
        return f"at {autonomy} autonomy the operator decides this plan: {BUTTONS}{advice}"
    if agent.lane != MASTER:
        return f"only the swarm master reviews a plan at {autonomy} autonomy"
    return ""


def decide(ledger, slug, agent, autonomy, phase_id, action, note, override=""):
    doc = ledger.state(slug)
    phase = next((p for p in doc["phases"] if p["id"] == phase_id), None)
    if phase is None:
        raise SwarmError(f"phase {phase_id} is not in ledger {slug}")
    reason = refusal(agent, autonomy, phase, doc)
    if reason:
        raise SwarmError(reason)
    problems = slice_check.check(phase, doc, slice_check.Limits.from_env(os.environ))
    override = override.strip() if problems and action == "approve" else ""
    if problems and action == "approve" and not override:
        raise SwarmError(faulted(phase_id, problems))
    record = {"reason": override, "problems": problems} if override else None
    ledger.review_phase(slug, phase_id, DECISIONS[action], by=agent.name, note=note, override=record)
    review = next(p for p in ledger.state(slug)["phases"] if p["id"] == phase_id)["review"]
    result = {"phase": phase_id, **{k: review[k] for k in ("state", "rounds", "escalated") if k in review}}
    return {**result, "problems": problems, **({"override": override} if override else {})}


def faulted(phase_id, problems):
    count = f"{len(problems)} problem{'s' if len(problems) > 1 else ''}"
    return (
        f"the slice check lists {count} for phase {phase_id}: {' '.join(problems)} "
        'Fix the slice, send it back, or approve with --override "<reason>".'
    )


def review_line(me, autonomy):
    if autonomy == MANUAL:
        return (
            "- Review a phase plan in review only with a comment on the phase: at manual autonomy the operator "
            "approves or sends it back from the ledger page."
        )
    if autonomy == ASSIST:
        return (
            f"- When a phase plan is in review, {RECOMMEND}: at assist autonomy the operator approves or sends it "
            "back from the ledger page."
        )
    return (
        f"- When a phase plan is in review, approve it with {me} plan approve <phase id> or send it back with "
        f'{me} plan send-back <phase id> --note "<what to change>". After three send backs the operator decides.'
    )
