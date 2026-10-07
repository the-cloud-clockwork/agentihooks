"""The stored quota capacity decision as swarm status lines, and agents still working on a draining account."""

from scripts.inbox.store import InboxError, InboxStore
from scripts.swarm import capacity
from scripts.swarm.health.findings import MINUTE_MS, Finding

DRAINING = ("DRAIN", "BLOCKED")


def routing_left(account: dict) -> float | None:
    if account["five_left"] is None or account["week_left"] is None:
        return None
    return min(account["five_left"], account["week_left"])


def _left(value: float | None) -> str:
    return "unknown" if value is None else f"{value:g}% left"


def _state(account: dict) -> str:
    return account["state"].lower().replace("_", " ")


def _changed(at: int, now_ms: int) -> str:
    minutes = (now_ms - at) // MINUTE_MS
    if minutes < 1:
        return "changed just now"
    return f"changed {minutes} minute ago" if minutes == 1 else f"changed {minutes} minutes ago"


def lines(decision: dict, now_ms: int) -> list[str]:
    if not decision:
        return [capacity.status_line(decision)]
    caps = ", ".join(
        f"{lane} {decision['effective'][lane]} of {decision['configured'][lane]}" for lane in capacity.LANES
    )
    head = f"quota capacity {caps}, {_changed(decision['at'], now_ms)}, because {decision['reason']}"
    return [head] + [
        f"quota account {row['harness']} {row['name']}  {_state(row)}  routing {_left(routing_left(row))}"
        f"  sessions {row['sessions']}"
        for row in decision["accounts"]
    ]


def findings(store, slug: str, limits, now_ms: int) -> list[Finding]:
    accounts = {(row["harness"], row["name"]): row for row in capacity.read(store, slug).get("accounts", [])}
    warned = store.redis.hgetall(store.key(slug, "quota-warnings"))
    inbox, found = InboxStore(store.redis), []
    for agent in store.agents(slug):
        account = accounts.get((agent.harness, agent.account))
        if agent.state != "working" or agent.idle_ticks or agent.name not in warned:
            continue
        if account is None or account["state"] not in DRAINING:
            continue
        try:
            minutes = (now_ms - inbox.get(warned[agent.name]).created_at) // MINUTE_MS
        except InboxError:
            continue
        if minutes < limits.drain_minutes:
            continue
        found.append(
            Finding(
                "working on drain",
                agent.name,
                f"still working on {agent.harness} account {agent.account} {minutes} minutes after its quota handoff warning",
                (
                    f"account {agent.account} is {_state(account)}, routing {_left(routing_left(account))}",
                    f"task {agent.task}",
                ),
                f"{limits.drain_minutes} minutes after the quota handoff warning",
                minutes,
            )
        )
    return found
