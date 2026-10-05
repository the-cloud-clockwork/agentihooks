"""Mail left pending for a swarm agent that exits: moved to its seat when the work goes on, else withdrawn and its
sender told."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scripts.inbox.store import InboxStore
    from scripts.swarm.store import RedisStore

BY = "swarm"


def settle(inbox, name, seat, exit_text):
    """seat is where the work goes on, '' when nobody takes it up."""
    from scripts.inbox.store import CLOSED

    inbox.seats.record_exit(name, seat, exit_text)
    for item in inbox.inbox(name):
        if item.state in CLOSED:
            continue
        if seat:
            inbox.redirect(item.id, BY, seat, f"{name} {exit_text}; moved to {seat} for its next occupant", name)
        elif (
            inbox.withdraw(item.id, BY, f"cancelled: {name} {exit_text} before closing it", name) and item.sender != BY
        ):
            inbox.send(BY, notice_address(inbox, item.sender), _told(item, name, exit_text), fyi=True)


def notice_address(inbox: "InboxStore", sender: str) -> str:
    from scripts.inbox.seats import is_seat, master_of
    from scripts.swarm.store import RedisStore

    master = master_of(sender)
    if not master or is_seat(sender):
        return sender
    slug = master.split("@", 1)[1]
    store = RedisStore(inbox.redis)
    if not store.seats.known_seat(sender):
        return sender
    if any(agent.name == sender and agent.state != "finished" for agent in store.agents(slug)):
        return sender
    return master


def sweep(inbox: "InboxStore", slug: str, store: "RedisStore", rows: dict) -> None:
    active = {agent.name for agent in store.agents(slug) if agent.state != "finished"}
    tasks = {row.get("claimed_by"): row for row in rows.values()}
    for name, seat in store.seats.agent_seats(slug):
        if name in active:
            continue
        outcome = store.seats.exit_of(name)
        if outcome:
            settle(inbox, name, outcome["seat"], outcome["reason"])
            continue
        state = tasks.get(name, {}).get("state")
        if state in ("done", "blocked"):
            exit_text = "finished its task and exited" if state == "done" else "blocked its task and exited"
            settle(inbox, name, "", exit_text)
        else:
            settle(inbox, name, seat, "exited")


def _told(item, name, exit_text):
    gist = item.text.splitlines()[0][:200] if item.text else ""
    return (
        f"{name} {exit_text} before closing your message {item.id}: {gist}. "
        "It is closed; send it to whoever carries that work on if it still matters."
    )
