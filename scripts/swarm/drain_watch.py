"""Agents still working on a draining account past their quota handoff warning, a health finding for the master."""

from scripts import quota_freshness, quota_state
from scripts.inbox.store import InboxError, InboxStore
from scripts.swarm import capacity, quota_view
from scripts.swarm.health.findings import MINUTE_MS, Finding, Limits
from scripts.swarm.store import RedisStore


def draining(account: dict, limits: Limits, now: float) -> bool:
    resets = (account.get("five_resets_at"), account.get("week_resets_at"))
    if quota_freshness.stale(account.get("observed_at"), resets, now):
        return False
    return account["state"] == quota_state.CLOSED or quota_state.draining(
        quota_view.routing_left(account), limits.drain_left
    )


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
        if account is None or not draining(account, limits, now_ms / 1000):
            continue
        try:
            warned_at = inbox.get(warned[agent.name]).created_at
        except InboxError:
            continue
        minutes = (now_ms - warned_at) // MINUTE_MS
        if warned_at >= agent.started_at and minutes > limits.drain_minutes:
            found.append(_finding(agent, account, minutes, limits))
    return found
