"""Agents on an account whose token the provider revoked or rejects: no turn can run there, so the tick retires each
one at once and hands its seat on with a runtime handoff; the successor routes to an open account."""

from scripts.handoff import transfers
from scripts.swarm import capacity, master_stall

REASON = "revoked"
CAUSE = "the provider revoked the token of {harness} account {account}"
WORKER = """## Intent
Continue task {task}; {cause}.

## Done
Observed: the quota probe of account {account} was refused by the provider, so the tick retired {name}.

## Stopped at
unknown: {name} could not run a turn on the revoked account and left no handoff.

## Decisions and promises
Account {account} takes no launch until its token works again; this task continues on an open account.

## Next
Read the task progress and proof, then continue the task from its last verified step.

## Read first
workspace:{task}/progress
workspace:{task}/proof

<!-- handoff complete -->
"""
MASTER = """## Intent
Continue as the master of {slug}; {cause}.

## Done
Observed: the quota probe of account {account} was refused by the provider, so the swarm recycled {name}.

## Stopped at
unknown: {name} could not run a turn on the revoked account and left no handoff.

## Decisions and promises
unknown: none recorded outside the ledger and the master inbox.

## Next
Re-read the ledger and the master inbox, then continue as master.

## Read first
- inbox:master

<!-- handoff complete -->
"""


def accounts(store, slug: str) -> set[tuple[str, str]]:
    rows = capacity.read(store, slug).get("accounts", [])
    return {(row["harness"], row["name"]) for row in rows if row.get("revoked")}


def on_revoked(store, slug: str, agent) -> bool:
    return bool(agent.account) and (agent.harness, agent.account) in accounts(store, slug)


def _cause(agent) -> str:
    return CAUSE.format(harness=agent.harness, account=agent.account)


def worker_handoff(store, slug: str, agent, now_ms: int) -> dict:
    text = store.handoff(slug, agent.task) or WORKER.format(
        task=agent.task, cause=_cause(agent), account=agent.account, name=agent.name
    )
    return transfers.record(store, slug, agent, REASON, text, now_ms)


def recycle_master(slug: str, store, ledger, agent, now_ms: int) -> None:
    text = MASTER.format(slug=slug, cause=_cause(agent), account=agent.account, name=agent.name)
    master_stall.recycle(slug, store, ledger, agent, text, now_ms, REASON)
