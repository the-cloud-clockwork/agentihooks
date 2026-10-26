"""agentihooks swarm resolve: each blocker protocol step recorded on the item, counted by code.

The critic step launches a read only headless critic from code with only the blind prompt the ledger rebuilds from
the record, so the agent never writes the critic's input or picks who answers.
"""

import json

from scripts.swarm.store import SwarmError
from scripts.swarm_ledger import ledger_blockers

ACTIONS = ("attempt", "critic", "escalate", "status")
CRITIC_TIER = "high"
CRITIC_EFFORT = "medium"
CRITIC_SCHEMA = {
    "type": "object",
    "properties": {"resolution": {"type": "string", "minLength": 1}},
    "required": ["resolution"],
    "additionalProperties": False,
}


def headless_critic(prompt: str) -> tuple[str, str]:
    from scripts.headless import runner
    from scripts.headless.request import READ_ONLY, HeadlessError

    try:
        reply = runner.run(ledger_blockers.CRITIC_ROLE, prompt, CRITIC_SCHEMA, READ_ONLY, CRITIC_TIER, CRITIC_EFFORT)
    except (HeadlessError, ValueError) as exc:
        raise SwarmError(f"the blind critic did not answer: {exc}") from exc
    return f"headless {CRITIC_TIER} tier read only critic", reply["resolution"]


def _row(ledger, slug, item):
    row = ledger.item(slug, item)
    if row is None:
        raise SwarmError(f"ledger {slug} holds no item {item}")
    return row


def _fields(args):
    if args.action == "attempt":
        return {"text": args.tried, "command": args.ran, "output": args.output}
    return {"reason": args.reason, "evidence": args.evidence}


def run(store, args, by, ledger, critic=headless_critic):
    tier = store.config(args.slug).autonomy
    row = _row(ledger, args.slug, args.item)
    rec = ledger_blockers.record(row)
    if args.action == "critic":
        if ledger_blockers.next_step(rec) != ledger_blockers.CRITIC:
            raise SwarmError(ledger_blockers.step_text(args.slug, args.item, rec))
        prompt = ledger_blockers.blind_prompt(args.item, ledger_blockers.subject(row), tier, rec["attempts"])
        launched, resolution = critic(prompt)
        fields = {"run": launched, "digest": ledger_blockers.digest(prompt), "resolution": resolution}
        ledger.blocker(args.slug, "blocker_critic", by, args.item, fields)
    elif args.action != "status":
        missing = [name for name, value in _fields(args).items() if not (value or "").strip()]
        if missing:
            raise SwarmError(f"resolve {args.action} needs {' '.join('--' + m for m in missing)}")
        ledger.blocker(args.slug, f"blocker_{args.action}", by, args.item, _fields(args))
    rec = ledger_blockers.record(_row(ledger, args.slug, args.item))
    step = ledger_blockers.next_step(rec)
    print(
        json.dumps(
            {
                "item": args.item,
                "autonomy": tier,
                "attempts": len(rec["attempts"]),
                "critic": (rec["critic"] or {}).get("resolution"),
                "next": step,
                "do": ledger_blockers.step_text(args.slug, args.item, rec),
            }
        )
    )


def block_refusal(tier: str, slug: str, task: dict) -> str:
    item = f"tasks/{task['id']}"
    rec = ledger_blockers.record(task)
    if tier not in ledger_blockers.GATED or ledger_blockers.next_step(rec) == ledger_blockers.ESCALATED:
        return ""
    return (
        f"at {tier} autonomy a block reaches the operator only after the blocker protocol, so the next step is to "
        f"{ledger_blockers.step_text(slug, item, rec)}"
    )
