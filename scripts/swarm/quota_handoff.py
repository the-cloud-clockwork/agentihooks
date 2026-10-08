from dataclasses import dataclass

from scripts.inbox.store import InboxStore
from scripts.swarm import capacity
from scripts.swarm.store import RedisStore


@dataclass(frozen=True)
class Thresholds:
    week: float = 90
    five: float = 95

    @classmethod
    def from_env(cls, environ: dict) -> "Thresholds":
        thresholds = cls(
            float(environ.get("AGENTIHOOKS_HANDOFF_WARN_WEEK_PCT", "90")),
            float(environ.get("AGENTIHOOKS_HANDOFF_WARN_5H_PCT", "95")),
        )
        hard_week = float(environ.get("AGENTIHOOKS_HANDOFF_WEEK_PCT", "98"))
        hard_five = float(environ.get("AGENTIHOOKS_HANDOFF_5H_PCT", "99"))
        if not (0 < thresholds.week < hard_week and 0 < thresholds.five < hard_five):
            raise ValueError("quota warning thresholds must be positive and below the hard handoff thresholds")
        return thresholds


def trigger(account: capacity.Account, thresholds: Thresholds) -> str:
    if account.state == "UNKNOWN":
        return ""
    if account.week_left is not None and 100 - account.week_left >= thresholds.week:
        return "week"
    if account.five_left is not None and 100 - account.five_left >= thresholds.five:
        return "five hour"
    return ""


def directive(slug: str, account: capacity.Account, window: str) -> str:
    used = 100 - (account.week_left if window == "week" else account.five_left)
    return (
        f"QUOTA HANDOFF WARNING: {account.harness} account {account.name} has used {used:g}% of its {window} window. "
        "Finish your current step and write your Handoff v2 with the handoff skill while quota remains. "
        f"Submit it with agentihooks swarm {slug} handoff DOC --reason quota, then stop. "
        "The tick keeps your seat and task and routes the successor to an account with room. "
        "This warning does not block tools or terminate your running agent."
    )


def warn(slug: str, store: RedisStore, environ: dict) -> list[str]:
    thresholds = Thresholds.from_env(environ)
    accounts = {
        (row["harness"], row["name"]): capacity.Account(**row) for row in capacity.read(store, slug).get("accounts", [])
    }
    key, actions = store.key(slug, "quota-warnings"), []
    lives = store.key(slug, "quota-warning-lives")
    for agent in store.agents(slug):
        if agent.state == "finished" or store.redis.hget(lives, agent.name) == str(agent.started_at):
            continue
        account = accounts.get((agent.harness, agent.account))
        if account is None or not (window := trigger(account, thresholds)):
            continue
        item = InboxStore(store.redis).send("swarm", agent.name, directive(slug, account, window))
        store.redis.hset(key, agent.name, item.id)
        store.redis.hset(lives, agent.name, agent.started_at)
        actions.append(f"early quota handoff warning sent to {agent.name}")
    return actions


def exclusion(account: capacity.Account, thresholds: Thresholds, predecessor: tuple | None = None) -> str:
    if (account.harness, account.name) == predecessor:
        return "is the account handing off"
    if not capacity.free_seats(account):
        return "has no free seats"
    if account.state == "UNKNOWN" or (account.five_left is None and account.week_left is None):
        return "has no quota reading"
    if window := trigger(account, thresholds):
        return f"is at its {window} quota warning"
    return ""


def refusal(predecessor: tuple, harnesses: tuple, accounts: list[capacity.Account], reason) -> str:
    reasons = "; ".join(f"{row.harness} {row.name} {reason(row)}" for row in accounts)
    return (
        f"no {' or '.join(harnesses)} account can take the quota handoff from {predecessor[0]} account "
        f"{predecessor[1]}: {reasons or 'no accounts were observed'}"
    )


def successor(accounts: list[capacity.Account], allow_codex: bool, thresholds: Thresholds) -> capacity.Account | None:
    for harness in ("claude", "codex") if allow_codex else ("claude",):
        eligible = [row for row in accounts if row.harness == harness and not exclusion(row, thresholds)]
        if eligible:
            return min(
                eligible,
                key=lambda row: (
                    -min(value for value in (row.five_left, row.week_left) if value is not None),
                    row.name,
                ),
            )
    return None
