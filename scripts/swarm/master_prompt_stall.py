"""A master held on a question panel, a permission prompt or an interrupted prompt while operator items wait: the tick
asks it for a handoff through its inbox, then recycles it in its seat like a stalled master, which also dismisses an
open question panel. The reset enforcement starts log only. A master showing a Claude login limit is recycled at once,
outside the gate, so its relaunch takes another stored login."""

import os
from typing import Any

from scripts import master_claude_login
from scripts.gates import modes
from scripts.gates.base import Who
from scripts.gates.enforcement import MASTER_RESET as GATE
from scripts.inbox import wake
from scripts.inbox.seats import seat_address
from scripts.inbox.store import InboxStore
from scripts.swarm import affinity, idle, master_retire, master_stall
from scripts.swarm.delivery import READY
from scripts.swarm.health.findings import MINUTE_MS
from scripts.swarm.pane import interrupted_prompt, login_limit
from scripts.swarm.store import MASTER, AgentRecord

TAIL_LINES = 40
DAY_S = 86_400
WAKE_MINUTES = 10
RESET_MINUTES = 15
OPERATOR = "operator"
HELD = "held on "
INTERRUPTED = "an interrupted prompt"
REASON = HELD + "{prompt} with operator items waiting for {minutes} minutes"
RESUMED = "the master left the prompt"
RECYCLED = "the swarm recycled the master after fifteen minutes on the prompt"
LOGIN_LIMITED = "showed a Claude login that reached its time limit"
LOGIN_RECYCLE_KEY = "master-login-recycle"
LOGIN_RECYCLED = "the swarm recycled the master on a Claude login limit"
LOGIN_ALERT_KEY = "master-login-alert"
LOGIN_ALERT_SOURCE = "master-login"
LOGIN_ALERT = (
    "The master hit the Claude login limit again and no stored master login is left that passes its check; "
    "log in one master login slot again so the master can run."
)
LOGIN_RECOVERY = """## Intent
Continue as the master of {slug}; {name} {limited} and the swarm recycled it so the relaunch takes another stored login.

## Done
unknown: {name} could not run a turn once its login hit the limit and left no handoff document.

## Stopped at
unknown: {name} answered every turn with the login error and never reported its last step.

## Decisions and promises
unknown: none recorded outside the ledger and the master inbox.

## Next
Answer the open items in the master inbox first, then re-read the ledger and continue as master.

## Read first
- inbox:master

<!-- handoff complete -->
"""
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
    return ""


def _prompt(runtime, master):
    observed = runtime.observe(master)
    if observed.state != "idle":
        return held_on(observed)
    try:
        tail = runtime.pane_tail(master.pane_id, TAIL_LINES)
    except Exception:
        return ""
    return INTERRUPTED if interrupted_prompt(master.harness, tail) else ""


def _operator_waiting(store, slug, master):
    inbox = InboxStore(store.redis)
    mail = inbox.open_items(master.name) + inbox.open_items(seat_address(slug, MASTER))
    return any(item.sender == OPERATOR for item in mail)


def _clear(store, slug, master, key):
    store.redis.delete(key)
    if master_retire.reason(store, slug, master.name).startswith(HELD):
        master_retire.forget(store, slug, master.name, RESUMED)


def _recycled_lately(store: Any, slug: str, now_ms: int) -> bool:
    last = store.redis.get(store.key(slug, LOGIN_RECYCLE_KEY))
    return last is not None and now_ms - int(last) < RESET_MINUTES * MINUTE_MS


def _prompted(store: Any, slug: str, master: AgentRecord, now_ms: int) -> bool:
    last = idle.last_prompt(store.redis, slug, master.name)
    return last is not None and now_ms - last < wake.quiet_ms(os.environ)


def login_limited(slug: str, store: Any, runtime: Any, master: AgentRecord, now_ms: int) -> str:
    """The Claude login error a live master pane that ended its turn shows, or empty while the operator is at the pane."""
    observed = runtime.observe(master)
    if observed.state not in READY or observed.typed or _prompted(store, slug, master, now_ms):
        return ""
    try:
        tail = runtime.pane_tail(master.pane_id, TAIL_LINES)
    except Exception:
        return ""
    return login_limit(master.harness, tail)


def _recycle_login(slug: str, store: Any, ledger: Any, master: AgentRecord, key: str, now_ms: int) -> list[str]:
    master_retire.forget(store, slug, master.name, LOGIN_RECYCLED)
    GATE.allows(store, Who(name=master.name, swarm=slug), "recycle", LOGIN_LIMITED, now_ms)
    master_claude_login.mark_held_failed(master.name)
    text = LOGIN_RECOVERY.format(slug=slug, name=master.name, limited=LOGIN_LIMITED)
    master_stall.recycle(slug, store, ledger, master, text, now_ms, master_stall.STALLED)
    store.redis.delete(key)
    stamp = store.key(slug, LOGIN_RECYCLE_KEY)
    repeat = store.redis.get(stamp) is not None
    store.redis.set(stamp, now_ms, ex=DAY_S)
    return [
        f"recycled master {master.name} in its seat: {LOGIN_LIMITED}",
        *_alert_spent(slug, store, ledger, repeat, now_ms),
    ]


def _alert_spent(slug: str, store: Any, ledger: Any, repeat: bool, now_ms: int) -> list[str]:
    if not repeat or master_claude_login.spare():
        return []
    if not store.redis.set(store.key(slug, LOGIN_ALERT_KEY), now_ms, nx=True, ex=DAY_S):
        return []
    ledger.raise_alert(slug, LOGIN_ALERT_SOURCE, LOGIN_ALERT)
    return [f"raised the master login alert: {LOGIN_ALERT}"]


def run(slug: str, config: Any, store: Any, ledger: Any, runtime: Any, now_ms: int) -> list[str]:
    master = affinity.live_master(store, slug)
    if master is None or config.state in {"stopping", "stopped"}:
        return []
    key = store.key(slug, "master-prompt-stall", master.name)
    live = master.name in runtime.live_names()
    if live and not _recycled_lately(store, slug, now_ms) and login_limited(slug, store, runtime, master, now_ms):
        return _recycle_login(slug, store, ledger, master, key, now_ms)
    prompt = _prompt(runtime, master) if live and _operator_waiting(store, slug, master) else ""
    if not prompt:
        _clear(store, slug, master, key)
        return []
    store.redis.hsetnx(key, "since", now_ms)
    store.redis.expire(key, DAY_S)
    minutes = (now_ms - int(store.redis.hget(key, "since"))) // MINUTE_MS
    mode = modes.configured(GATE, config.gates)
    if minutes < WAKE_MINUTES or mode == "off":
        return []
    reason = REASON.format(prompt=prompt, minutes=minutes)
    who = Who(name=master.name, swarm=slug)
    if mode == "observe":
        stage = "recycle" if minutes >= RESET_MINUTES else "wake"
        if not store.redis.hsetnx(key, stage, now_ms):
            return []
        GATE.allows(store, who, stage, reason, now_ms)
        would = f"recycle {master.name}" if stage == "recycle" else f"ask {master.name} for a handoff"
        return [f"log only: would {would}, {reason}"]
    held_for = master_retire.reason(store, slug, master.name)
    if held_for and not held_for.startswith(HELD):
        return []
    if minutes < RESET_MINUTES:
        held = master_retire.hold(store, slug, master, held_for or reason, True, now_ms)
        if held and not held_for:
            GATE.allows(store, who, "wake", reason, now_ms)
        return [held] if held else []
    master_retire.forget(store, slug, master.name, RECYCLED)
    GATE.allows(store, who, "recycle", reason, now_ms)
    text = RECOVERY.format(slug=slug, name=master.name, minutes=minutes, prompt=prompt)
    master_stall.recycle(slug, store, ledger, master, text, now_ms, master_stall.STALLED)
    store.redis.delete(key)
    return [
        f"recycled master {master.name} in its seat after {minutes} minutes {HELD}{prompt} with operator items waiting"
    ]
