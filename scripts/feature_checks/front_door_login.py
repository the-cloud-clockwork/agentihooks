"""Live check of the front door login feature: every swarm's live front door master runs on the preferred stored
claude ai login, the same one for all, unless that login is spent. Read from the stored login slot owner records,
so it prints account names only, never a credential."""

import sys
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from scripts import master_claude_login as login
from scripts import master_logins, quota_state
from scripts.swarm_v2 import front_door

if TYPE_CHECKING:
    from scripts.swarm.store import RedisStore

TOKEN = "{slug} front door {name} runs on a quota token while the stored login {login} has room"
SPREAD = "front doors sit on different logins, {logins}, while the preferred login {login} has room"


@dataclass(frozen=True)
class FrontDoor:
    slug: str
    name: str
    login: str
    account: str


def _held() -> dict[str, str]:
    stored = login.logins()
    return {owner: stored[number] for number in stored for owner in login.holders(login.root(number))}


def front_doors(store: "RedisStore") -> list[FrontDoor]:
    held = _held()
    return [
        FrontDoor(slug, agent.name, held.get(agent.name, ""), agent.account)
        for slug in store.slugs()
        for agent in store.agents(slug)
        if agent.lane == login.LANE
        and agent.state == "working"
        and agent.harness == "claude"
        and front_door.holds(slug, agent.seat)
    ]


def ordered(now: float) -> list[tuple[str, float]]:
    """The stored logins in the order a front door launch tries them, each with its room left."""
    ranks = login._preferred()
    found = [(number, account, master_logins.left(account, now)) for number, account in login.logins().items()]
    found.sort(key=lambda row: (ranks.get(row[1], len(ranks)), -row[2], row[0]))
    return [(account, left) for _, account, left in found if account]


def violations(doors: list[FrontDoor], logins: list[tuple[str, float]]) -> list[str]:
    roomy = [account for account, left in logins if not quota_state.draining(left)]
    found = [TOKEN.format(slug=d.slug, name=d.name, login=roomy[0]) for d in doors if not d.login and roomy]
    used = sorted({d.login for d in doors if d.login})
    if len(used) > 1 and logins and logins[0][0] in roomy:
        found.append(SPREAD.format(logins=" and ".join(used), login=logins[0][0]))
    return found


def _line(door: FrontDoor) -> str:
    route = f"the claude ai login {door.login}" if door.login else f"a quota token {door.account or 'unknown'}"
    return f"{door.slug} front door {door.name} runs on {route}"


def main(store: "RedisStore | None" = None) -> int:
    if store is None:
        from scripts.swarm.store import RedisStore, redis_client

        store = RedisStore(redis_client())
    doors, logins = front_doors(store), ordered(time.time())
    print("stored logins in order: " + (", ".join(f"{a} {left:.0f}% left" for a, left in logins) or "none"))
    for door in doors:
        print(_line(door))
    found = violations(doors, logins)
    for line in found:
        print(f"FAIL: {line}")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
