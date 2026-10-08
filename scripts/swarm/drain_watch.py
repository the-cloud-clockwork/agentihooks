"""Agents still working on a closed account past their quota handoff warning, a health finding for the master."""

from scripts.inbox.store import InboxError, InboxStore
from scripts.swarm import capacity, quota_view
from scripts.swarm.health.findings import MINUTE_MS, Finding, Limits
from scripts.swarm.store import RedisStore

DRAINING = "CLOSED"


def _finding(agent, account: dict, minutes: int, limits: Limits) -> Finding:
    routing = quota_view.left_text(quota_view.routing_left(account))
    return Finding(
        "working on drain",
        agent.name,
        f"still working on {agent.harness} account {agent.account} {minutes} minutes after its quota handoff warning",
        (f"account {agent.account} is {quota_view.state_text(account)}, routing {routing}", f"task {agent.task}"),
        f"over {limits.drain_minutes} minutes after the quota handoff warning",
        minutes,
    )


def findings(store: RedisStore, slug: str, limits: Limits, now_ms: int) -> list[Finding]:
    accounts = {(row["harness"], row["name"]): row for row in capacity.read(store, slug).get("accounts", [])}
    warned = store.redis.hgetall(store.key(slug, "quota-warnings"))
    inbox, found = InboxStore(store.redis), []
    for agent in store.agents(slug):
        account = accounts.get((agent.harness, agent.account))
        if agent.state != "working" or agent.idle_ticks or agent.name not in warned:
            continue
        if account is None or account["state"] != DRAINING:
            continue
        try:
            minutes = (now_ms - inbox.get(warned[agent.name]).created_at) // MINUTE_MS
        except InboxError:
            continue
        if minutes > limits.drain_minutes:
            found.append(_finding(agent, account, minutes, limits))
    return found
