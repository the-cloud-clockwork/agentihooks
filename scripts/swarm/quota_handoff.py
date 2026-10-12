from collections.abc import Callable
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


def windows(account: capacity.Account, thresholds: Thresholds) -> list[str]:
    if account.kind == "api" or account.state == "UNKNOWN":
        return []
    return [
        window
        for window, left, limit in (
            ("week", account.week_left, thresholds.week),
            ("five hour", account.five_left, thresholds.five),
        )
        if left is not None and 100 - left >= limit
    ]


def trigger(account: capacity.Account, thresholds: Thresholds) -> str:
    return next(iter(windows(account, thresholds)), "")


def _reset(account: capacity.Account, window: str) -> int | None:
    return account.week_resets_at if window == "week" else account.five_resets_at


def _window_text(account: capacity.Account, window: str) -> str:
    from hooks.context.quota_policy import reset_when

    left = account.week_left if window == "week" else account.five_left
    reset = _reset(account, window)
    when = f"it resets {reset_when(reset)}" if reset else "its reset time is unknown"
    return f"has used {100 - left:g}% of its {window} window, {left:g}% left; {when}"


def directive(slug: str, account: capacity.Account, crossed: list[str]) -> str:
    return (
        f"QUOTA HANDOFF WARNING: {account.harness} account {account.name} "
        f"{'. It '.join(_window_text(account, window) for window in crossed)}. "
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
    lives, periods = store.key(slug, "quota-warning-lives"), store.key(slug, "quota-warning-periods")
    for agent in store.agents(slug):
        account = accounts.get((agent.harness, agent.account))
        if agent.state == "finished" or account is None:
            continue
        life, due = str(agent.started_at), []
        for window in windows(account, thresholds):
            field, period = f"{agent.name}:{window}", f"{life}:{_reset(account, window)}"
            if store.redis.hget(periods, field) != period:
                due.append((window, field, period))
        if not due:
            continue
        crossed = [window for window, _, _ in due]
        item = InboxStore(store.redis).send("swarm", agent.name, directive(slug, account, crossed))
        if store.redis.hget(lives, agent.name) != life:
            store.redis.hset(key, agent.name, item.id)
        store.redis.hset(lives, agent.name, life)
        store.redis.hset(periods, mapping={field: period for _, field, period in due})
        actions.append(f"early quota handoff warning sent to {agent.name}")
    return actions


def exclusion(account: capacity.Account, thresholds: Thresholds, predecessor: tuple | None = None) -> str:
    if (account.harness, account.name) == predecessor:
        return "is the account handing off"
    if not capacity.free_seats(account):
        return "has no free seats"
    if account.kind == "api":
        return ""
    if account.state == "UNKNOWN" or (account.five_left is None and account.week_left is None):
        return "has no quota reading"
    if window := trigger(account, thresholds):
        return capacity.warning(window)
    return ""


def refusal(
    predecessor: tuple, harnesses: tuple, accounts: list[capacity.Account], reason: Callable[[capacity.Account], str]
) -> str:
    reasons = "; ".join(f"{row.harness} {row.name} {reason(row)}" for row in accounts)
    return (
        f"no {' or '.join(harnesses)} account can take the quota handoff from {predecessor[0]} account "
        f"{predecessor[1]}: {reasons or 'no accounts were observed'}"
    )


def successor(
    accounts: list[capacity.Account], harnesses: tuple[str, ...], thresholds: Thresholds
) -> capacity.Account | None:
    for harness in harnesses:
        eligible = [row for row in accounts if row.harness == harness and not exclusion(row, thresholds)]
        if eligible:
            return min(
                eligible,
                key=lambda row: (
                    -100
                    if row.kind == "api"
                    else -min(value for value in (row.five_left, row.week_left) if value is not None),
                    row.name,
                ),
            )
    return None
