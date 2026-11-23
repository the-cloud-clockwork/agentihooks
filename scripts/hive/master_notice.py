"""The master seat items a hive drain or a hive loss sends to each swarm it served, once per hive and change."""

import hashlib
from typing import TYPE_CHECKING, Any, cast

from scripts.hive import purge, scope
from scripts.inbox.seats import seat_address  # pyright: ignore[reportUnknownVariableType]

if TYPE_CHECKING:
    from scripts.events.envelope import Envelope

SENDER = "swarm"
DRAINED = "hive {hive} is draining: {reason}. Agents of this swarm on it: {agents}"
LOST = "hive {hive} was lost: {reason}. The hub ended {ended}. Agents of this swarm on it: {agents}"


def once(*parts: object) -> str:
    """The inbox item id of one notice, the same for the same parts."""
    return hashlib.sha256(":".join(str(part) for part in parts).encode()).hexdigest()[:12]


def send(redis: Any, slug: str, text: str, item_id: str) -> None:
    """Leave one fyi item at the master seat of ``slug``; a second send of the same item id sends nothing.

    A process that talks Redis as a hive member sends nothing, because the inbox relay refuses a member's item whose
    sender that member does not run, so the item would never reach the master."""
    from scripts.inbox.store import DuplicateItem, InboxStore

    if scope.member():
        return
    try:
        address: str = seat_address(slug, "master")
        InboxStore(redis).send(SENDER, address, text, fyi=True, item_id=item_id)
    except DuplicateItem:
        pass


def drained(store: Any, hive: str, record: dict[str, Any]) -> None:
    """Tell the master of each swarm with a live agent on ``hive`` that the drain in ``record`` began, once per drain."""
    drains: list[dict[str, Any]] = record.get("drains") or []
    if record.get("draining") != "yes" or not drains or drains[-1]["draining"] != "yes":
        return
    latest = drains[-1]
    served: dict[str, list[str]] = {}
    for slug, agent in purge.hosted(store, hive):
        served.setdefault(slug, []).append(agent.name)
    for slug, names in served.items():
        text = DRAINED.format(hive=hive, reason=latest["reason"], agents=", ".join(sorted(names)))
        send(store.redis, slug, text, once("drain", hive, slug, latest["at"], latest["owner"], latest["reason"]))


def _data(exit: "Envelope") -> dict[str, Any]:
    return cast(dict[str, Any], exit.data)


def ended(exits: list["Envelope"]) -> str:
    """The executions and tasks a hive loss ended, in the hub's words."""
    return ", ".join(f"execution {exit.key} of task {_data(exit)['task']}" for exit in exits)


def lost(redis: Any, swarm: str, hive: str, exits: list["Envelope"]) -> None:
    """Tell the master of ``swarm`` that the hub ended ``exits`` on the lost ``hive``, once per loss."""
    names = sorted({str(_data(exit)["name"]) for exit in exits if _data(exit).get("name")})
    reason = str(_data(exits[0])["reason"])
    text = LOST.format(hive=hive, reason=reason, ended=ended(exits), agents=", ".join(names) or "none named")
    send(redis, swarm, text, once("lost", hive, swarm, *sorted(exit.id for exit in exits)))
