"""A master held on a question panel, a permission prompt or an interrupted prompt while operator items wait: the tick
asks it for a handoff through its inbox, then recycles it in its seat like a stalled master, which also dismisses an
open question panel. The master-reset enforcement starts log only."""

from scripts.gates import modes
from scripts.gates.base import Who
from scripts.gates.enforcement import MASTER_RESET as GATE
from scripts.inbox.seats import seat_address
from scripts.inbox.store import InboxStore
from scripts.swarm import affinity, master_retire, master_stall
from scripts.swarm.health.findings import MINUTE_MS
from scripts.swarm.store import MASTER

WAKE_MINUTES = 10
RESET_MINUTES = 15
OPERATOR = "operator"
HELD = "held on "
REASON = HELD + "{prompt} with operator items waiting for {minutes} minutes"
RESUMED = "the master left the prompt"
RECOVERY = """## Intent
Continue as the master of {slug}; {name} sat {minutes} minutes on {prompt} while operator items waited, and the swarm recycled it.

## Done
unknown: {name} left no handoff document before it was recycled.

## Stopped at
unknown: {name} was held on {prompt} and never reported its last step.

## Decisions and promises
unknown: none recorded outside the ledger and the master inbox.

## Next
Answer the open operator items in the master inbox first, then re-read the ledger and continue as master.

## Read first
- inbox:master

<!-- handoff complete -->
"""


def held_on(observed) -> str:
    if observed.state == "waiting":
        return f"the prompt {observed.prompt_title or 'waiting on input'}"
    if observed.state == "idle" and observed.interrupted:
        return "an interrupted prompt"
    return ""


def _oldest_operator_item(store, slug, master):
    inbox = InboxStore(store.redis)
    mail = inbox.open_items(master.name) + inbox.open_items(master.seat or seat_address(slug, MASTER))
    return min((item.created_at for item in mail if item.sender == OPERATOR), default=None)


def _clear(store, slug, master, key):
    store.redis.delete(key)
    if master_retire.reason(store, slug, master.name).startswith(HELD):
        master_retire.forget(store, slug, master.name, RESUMED)


def run(slug, config, store, ledger, runtime, now_ms):
    master = affinity.live_master(store, slug)
    if master is None or master.lane != MASTER or config.state in {"stopping", "stopped"}:
        return []
    key = store.key(slug, "master-prompt-stall", master.name)
    prompt = held_on(runtime.observe(master)) if master.name in runtime.live_names() else ""
    oldest = _oldest_operator_item(store, slug, master) if prompt else None
    if oldest is None:
        _clear(store, slug, master, key)
        return []
    store.redis.hsetnx(key, "since", now_ms)
    minutes = (now_ms - max(int(store.redis.hget(key, "since")), oldest)) // MINUTE_MS
    mode = modes.configured(GATE, config.gates)
    if minutes < WAKE_MINUTES or mode == "off":
        return []
    reason = REASON.format(prompt=prompt, minutes=minutes)
    who = Who(name=master.name, swarm=slug, lane=MASTER)
    if mode == "observe":
        stage = "recycle" if minutes >= RESET_MINUTES else "wake"
        if not store.redis.hsetnx(key, stage, now_ms):
            return []
        GATE.allows(store, who, stage, reason, now_ms)
        would = f"recycle {master.name}" if stage == "recycle" else f"ask {master.name} for a handoff"
        return [f"log only: would {would}, {reason}"]
    asking = not master_retire.reason(store, slug, master.name)
    if held := master_retire.hold(store, slug, master, reason, True, now_ms):
        if asking:
            GATE.allows(store, who, "wake", reason, now_ms)
        return [held]
    if minutes < RESET_MINUTES:
        return []
    GATE.allows(store, who, "recycle", reason, now_ms)
    text = RECOVERY.format(slug=slug, name=master.name, minutes=minutes, prompt=prompt)
    master_stall.recycle(slug, store, ledger, master, text, now_ms)
    store.redis.delete(key)
    return [f"recycled master {master.name} in its seat after {minutes} minutes {HELD}{prompt} with operator items waiting"]
