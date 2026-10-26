"""The blocker protocol: two attempts, a code launched blind critic, one attempt applying its resolution, then an
escalation with the blocker reason and its evidence.

Code records each step on the item's blocker record and counts them. At delegate or full autonomy an agent puts an
item before the operator only once its record holds the whole protocol; the escalation lands the priority and one
comment listing every attempt, the critic resolution and the blocker reason.
"""

import hashlib
import re

from scripts.swarm_ledger import ledger_freezes, ports

OPS = ("blocker_attempt", "blocker_critic", "blocker_escalate")
ITEM_RE = re.compile(r"^(phases|questions|followups|tasks)/[^/]+$")
AUTHOR_RE = re.compile(r"^[A-Za-z][\w.@-]{0,63}$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
FIELDS = {
    "blocker_attempt": ("item", "text", "command", "output"),
    "blocker_critic": ("item", "run", "digest", "resolution"),
    "blocker_escalate": ("item", "reason", "evidence"),
}
GATED = ("delegate", "full")
SERVICE = ("swarm", "ledger")
BEFORE_CRITIC = 2
MAX_TEXT = 8000
PRIORITY_WORDS = 14
ATTEMPT, CRITIC, ESCALATE, ESCALATED = "attempt", "critic", "escalate", "escalated"
NEEDS_OPERATOR = re.compile(r"^(followups/[^/]+)/needs_operator$")
CRITIC_ROLE = (
    "You are a blind critic for an autonomous agent. You see only the problem, the autonomy tier and what was tried, "
    "with each command and its output. Propose one concrete resolution the agent can apply itself, different from "
    "every attempt below. The agent must stay autonomous: never propose asking the operator."
)


def autonomy(slug: str) -> str:
    return ledger_freezes.autonomy(slug)


def find(doc: dict, item: str) -> dict | None:
    name, item_id = item.split("/")
    return next((row for row in doc.get(name) or () if row.get("id") == item_id), None)


def record(row: dict | None) -> dict:
    return {"attempts": [], "critic": None, "escalation": None, **((row or {}).get("blocker") or {})}


def subject(row: dict | None) -> str:
    return (row or {}).get("title") or (row or {}).get("text", "")


def next_step(rec: dict) -> str:
    before = [a for a in rec["attempts"] if not a.get("after_critic")]
    if rec["critic"] is None:
        return ATTEMPT if len(before) < BEFORE_CRITIC else CRITIC
    if len(before) == len(rec["attempts"]):
        return ATTEMPT
    return ESCALATE if rec["escalation"] is None else ESCALATED


def step_text(slug: str, item: str, rec: dict) -> str:
    step = next_step(rec)
    if step == ATTEMPT:
        which = "apply the critic resolution" if rec["critic"] else f"attempt {len(rec['attempts']) + 1} of 2"
        return (
            f'{which}: agentihooks swarm {slug} resolve attempt {item} --tried "<what you tried>" '
            f'--command "<the command run>" --output "<its output>"'
        )
    if step == CRITIC:
        return f"launch the blind critic: agentihooks swarm {slug} resolve critic {item}"
    if step == ESCALATE:
        return (
            f"escalate with the blocker reason and its evidence: agentihooks swarm {slug} resolve escalate {item} "
            f'--reason "<why it is blocked, in plain words>" --evidence "<the command and output that show it>"'
        )
    return "the protocol is complete and the item waits on the operator"


def notice(slug: str, tier: str, item: str, rec: dict) -> str:
    if tier in GATED:
        return (
            f"Autonomy {tier}: take the decision yourself, with no question to the operator. "
            f"On a blocker, the next protocol step is to {step_text(slug, item, rec)}."
        )
    return f"Autonomy {tier or 'unknown'}. Triage it: resolve it if the call is yours, else leave it for the operator."


def _attempt_lines(attempts: list) -> list[str]:
    return [
        f"Attempt {n}{' after the critic' if a.get('after_critic') else ''}: {a['text']}\n"
        f"Command: {a['command']}\nOutput: {a['output']}"
        for n, a in enumerate(attempts, 1)
    ]


def blind_prompt(item: str, problem: str, tier: str, attempts: list) -> str:
    return "\n\n".join(
        [CRITIC_ROLE, f"Item: {item}\nProblem: {problem}\nAutonomy: {tier or 'unknown'}", *_attempt_lines(attempts)]
    )


def digest(prompt: str) -> str:
    return hashlib.sha256(prompt.encode()).hexdigest()


def check(op):
    if set(op) - {"op", "id", "by", *FIELDS[op["op"]]}:
        raise ValueError(f"{op['op']} takes only by {' '.join(FIELDS[op['op']])}")
    by = op.get("by")
    if not isinstance(by, str) or not AUTHOR_RE.match(by) or by == "operator":
        raise ValueError(f"{op['op']} is written by an agent")
    if not ITEM_RE.match(str(op.get("item"))):
        raise ValueError(f"{op['op']} needs item <list>/<id>")
    for key in FIELDS[op["op"]][1:]:
        if not isinstance(op.get(key), str) or not op[key].strip() or len(op[key]) > MAX_TEXT:
            raise ValueError(f"{op['op']} needs {key} as text of up to {MAX_TEXT} characters")
    if op["op"] == "blocker_critic" and not DIGEST_RE.match(op["digest"]):
        raise ValueError("blocker_critic digest is the sha256 of the blind prompt")
    if op["op"] == "blocker_escalate":
        import ledger_comments

        ledger_comments.check(_priority_text(op["reason"]), "priority")


def _priority_text(reason: str) -> str:
    words = reason.split()
    return "Blocked: " + " ".join(words[:PRIORITY_WORDS]) + ("…" if len(words) > PRIORITY_WORDS else "")


def _refuse(ctx, item, rec, why):
    ctx.refused.append(f"{why}; {step_text(ctx.slug, item, rec)}")
    return False


def _attempt(row, rec, op, ctx):
    step = next_step(rec)
    if step == CRITIC:
        return _refuse(ctx, op["item"], rec, "two attempts are recorded before the critic")
    if step in (ESCALATE, ESCALATED):
        return _refuse(ctx, op["item"], rec, "one attempt applies the critic resolution")
    entry = {key: op[key].strip() for key in FIELDS[op["op"]][1:]}
    rec["attempts"].append({"id": op["id"], "by": op["by"], "at": ctx.at, **entry, "after_critic": bool(rec["critic"])})
    ctx.record(op["by"], "blocker attempt", op["item"], id=op["id"], text=entry["text"])
    return True


def _critic(row, rec, op, ctx):
    if next_step(rec) != CRITIC:
        return _refuse(ctx, op["item"], rec, "the critic runs once, after two attempts")
    expected = digest(blind_prompt(op["item"], subject(row), autonomy(ctx.slug), rec["attempts"]))
    if op["digest"] != expected:
        return _refuse(ctx, op["item"], rec, "the critic record does not match the code built blind prompt")
    rec["critic"] = {"id": op["id"], "by": op["by"], "at": ctx.at, "run": op["run"], "digest": op["digest"]}
    rec["critic"]["resolution"] = op["resolution"].strip()
    ctx.record(op["by"], "blocker critic", op["item"], id=op["id"], text=rec["critic"]["resolution"])
    return True


def _comment(rec, reason, evidence):
    lines = ["Blocker protocol record.", *_attempt_lines(rec["attempts"])]
    if rec["critic"]:
        lines.append(f"Critic resolution ({rec['critic']['run']}): {rec['critic']['resolution']}")
    return "\n\n".join([*lines, f"Blocker reason: {reason}\nEvidence: {evidence}"])


def _escalate(doc, row, rec, op, ctx):
    tier = autonomy(ctx.slug)
    step = next_step(rec)
    if tier in GATED and step != ESCALATE:
        why = "the item has already escalated" if step == ESCALATED else "the item escalates only after the protocol"
        return _refuse(ctx, op["item"], rec, f"at {tier} autonomy {why}")
    by, item = op["by"], op["item"]
    reason, evidence = op["reason"].strip(), op["evidence"].strip()
    rec["escalation"] = {"id": op["id"], "by": by, "at": ctx.at, "reason": reason, "evidence": evidence}
    text = _priority_text(reason)
    rows = [p for p in doc.setdefault("priorities", []) if p["item"] != item]
    doc["priorities"] = rows + [{"id": op["id"], "item": item, "text": text, "by": by, "at": ctx.at}]
    entry = {"id": f"{op['id']}-record", "by": by, "at": ctx.at, "text": _comment(rec, reason, evidence)}
    row.setdefault("comments", []).append(entry)
    ctx.record(by, "comment added", item, id=entry["id"], text=entry["text"])
    ctx.record(by, "priority added", item, id=op["id"], text=text)
    return True


def apply(doc, op, ctx):
    row = find(doc, op["item"])
    if row is None:
        return False
    rec = record(row)
    if op["op"] == "blocker_attempt":
        done = _attempt(row, rec, op, ctx)
    elif op["op"] == "blocker_critic":
        done = _critic(row, rec, op, ctx)
    else:
        done = _escalate(doc, row, rec, op, ctx)
    if done:
        row["blocker"] = rec
        ctx.stamp(op["item"], op["by"])
    return done


def asked_item(op: dict) -> str:
    if op["op"] == "priority":
        return op["item"]
    found = NEEDS_OPERATOR.match(op["path"]) if op["op"] == "set" else None
    return found.group(1) if found else ""


class Gate:
    """At delegate or full autonomy, an agent write that puts an item before the operator waits on the protocol."""

    def __init__(self, slug: str, inner=None) -> None:
        self.slug, self.inner = slug, inner

    def apply(self, doc: dict, op: dict, ctx, apply) -> bool:
        refused = self.refusal(doc, op)
        if refused:
            ctx.refused.append(refused)
            return False
        return self.inner.apply(doc, op, ctx, apply) if self.inner else apply(doc, op, ctx)

    def refusal(self, doc: dict, op: dict) -> str:
        by = op.get("by")
        if by is None or by in SERVICE or not ports.swarm().front_door.asks_operator(op):
            return ""
        tier = autonomy(self.slug)
        if tier not in GATED:
            return ""
        item = asked_item(op)
        rec = record(find(doc, item)) if item else record(None)
        if item and next_step(rec) == ESCALATED:
            return ""
        return (
            f"at {tier} autonomy {by} takes the decision itself; an item reaches the operator only after the blocker "
            f"protocol{'' if item else ' of the existing item it concerns, never a new question or follow up'}, so "
            f"the next step is to {step_text(self.slug, item or '<the item it concerns>', rec)}"
        )
