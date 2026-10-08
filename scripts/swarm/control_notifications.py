import os
import uuid
from argparse import Namespace

from scripts.inbox.seats import seat_address
from scripts.inbox.store import InboxStore
from scripts.swarm.ledger_client import LedgerClient
from scripts.swarm.store import MASTER, AgentRecord, RedisStore

CONTROL_REF = "swarm-control:"
CONTROLS = {
    "start": "started the swarm",
    "pause": "paused the swarm",
    "stop": "stopped the swarm",
    "close": "requested closing the ledger",
    "reopen": "reopened the ledger",
    "set": "changed the swarm settings",
    "verdict": "gave a health finding a verdict",
    "doctor_start": "started the Doctor",
    "doctor_stop": "stopped the Doctor",
}


def master(store: RedisStore, slug: str) -> AgentRecord | None:
    return next((a for a in store.agents(slug) if a.lane == MASTER and a.state != "finished"), None)


def notify(
    store: RedisStore, args: Namespace, ledger: LedgerClient, before: AgentRecord | None, action: str, detail: str = ""
) -> None:
    by = getattr(args, "name", "") or os.environ.get("AGENTIHOOKS_AGENT_NAME") or "operator"
    after = master(store, args.slug)
    if any(a and by == a.name for a in (before, after)):
        return
    boss = after or before
    address = (boss.seat or boss.name) if boss else seat_address(args.slug, MASTER)
    actor = "The operator" if by == "operator" else by.replace("-", " ").replace("@", " ")
    source = "page" if os.environ.get("AGENTIHOOKS_CONTROL_SOURCE") == "page" else "command line"
    verb = CONTROLS[action]
    if action == "stop" and args.now:
        verb = "stopped the swarm immediately"
    config = store.config(args.slug)
    result = f"The swarm is {config.state}."
    if action == "set":
        result += f" Engineer cap {config.max_eng}, CI cap {config.max_ci}, Planner cap {config.max_plan}."
    if action == "verdict":
        result += f" The verdict is {args.verdict.replace('-', ' ')}."
    text = f"{actor} {verb} from the {source}. {result}" + (f" {detail}" if detail else "")
    ledger.say(args.slug, text, by="swarm")
    if after or action not in {"stop", "close"}:
        InboxStore(store.redis).send(by, address, text, ref=f"{CONTROL_REF}{uuid.uuid4().hex}", fyi=True)
