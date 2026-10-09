"""Mail left pending for a swarm agent that exits: moved to its seat when the work goes on, else withdrawn and its
sender told."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from scripts.inbox.store import InboxStore
    from scripts.swarm.store import RedisStore

BY = "swarm"
UNKNOWN_OWNER = "the agent it was sent to is unknown, so no branch was checked"


def settle(inbox, name, seat, exit_text):
    """seat is where the work goes on, '' when nobody takes it up. A master's mail passes to its successor, or waits
    for one while none is spawned yet."""
    from scripts.inbox.store import CLOSED
    from scripts.swarm.naming import NameRegistry

    inbox.seats.record_exit(name, seat, exit_text)
    names = NameRegistry(inbox.redis)
    successor = names.successor(name) if names.entry(name).get("type") == "master" else ""
    for item in inbox.open_items(name):
        if item.state in CLOSED:
            continue
        if ended := _life_notice_end(item, name, exit_text):
            inbox.close(item.id, BY, "done", ended)
        elif successor:
            inbox.redirect(item.id, BY, successor, f"{name} {exit_text}; passed to {successor}, its successor", name)
        elif seat:
            inbox.redirect(item.id, BY, seat, f"{name} {exit_text}; moved to {seat} for its next occupant", name)
        elif names.entry(name).get("type") == "master":
            continue
        elif (
            inbox.withdraw(item.id, BY, f"cancelled: {name} {exit_text} before closing it", name) and item.sender != BY
        ):
            inbox.send(BY, notice_address(inbox, item.sender), _told(item, name, exit_text), fyi=True)
    if names.entry(name).get("type") != "master":
        _settle_taken_seat_mail(inbox, name, exit_text, bool(seat))


def _settle_taken_seat_mail(inbox: "InboxStore", name: str, exit_text: str, goes_on: bool) -> None:
    held = inbox.seats.known_seat(name)
    if not held:
        return
    for item in inbox.open_items(held):
        if item.state != "pending" and _taker(inbox, item) == name:
            _settle_taken(inbox, item, held, name, exit_text, goes_on)


def _settle_taken(inbox: "InboxStore", item, held: str, name: str, exit_text: str, goes_on: bool) -> None:
    """Seat mail a gone life took: back to pending for the seat's next occupant when the work goes on, else closed."""
    if ended := _life_notice_end(item, name, exit_text):
        inbox.close(item.id, BY, "done", ended)
    elif goes_on:
        inbox.redirect(item.id, BY, held, f"{name} {exit_text}; back to {held} for its next occupant", held, name)
    elif (
        inbox.withdraw(item.id, BY, f"cancelled: {name} {exit_text} before closing it", held, name)
        and item.sender != BY
    ):
        inbox.send(BY, notice_address(inbox, item.sender), _told(item, name, exit_text), fyi=True)


def _life_notice_end(item, name: str, exit_text: str) -> str:
    """The close reason of a notice meant only for the life that left; '' for any other item."""
    from scripts.gates import push_stop
    from scripts.swarm import quota_notice

    if push_stop.is_notice(item):
        return push_stop.left(name, exit_text)
    if quota_notice.is_notice(item):
        return f"{name} {exit_text}; its quota notice ended with it"
    return ""


def _taker(inbox, item):
    return next(
        (entry["by"] for entry in reversed(inbox.history(item.id)) if entry["state"] in ("delivered", "read")), ""
    )


def close_swarm(inbox: "InboxStore", slug: str) -> None:
    from scripts.inbox.seats import of_swarm
    from scripts.inbox.store import CLOSED

    prefix = inbox.key("address", "")
    for key in inbox.redis.scan_iter(match=prefix + "*"):
        address = key[len(prefix) :]
        if not of_swarm(address, slug, inbox.names):
            continue
        for item in inbox.open_items(address):
            if item.state not in CLOSED:
                inbox.withdraw(item.id, BY, f"cancelled: swarm closed; {address} has no further work", address)


def notice_address(inbox: "InboxStore", sender: str) -> str:
    from scripts.inbox.seats import is_seat, master_of
    from scripts.swarm.store import RedisStore

    store = RedisStore(inbox.redis)
    master = master_of(sender, store.names)
    if not master or is_seat(sender):
        return sender
    slug = master.split("@", 1)[1]
    store = RedisStore(inbox.redis)
    if not store.seats.known_seat(sender):
        return sender
    if any(agent.name == sender and agent.state != "finished" for agent in store.agents(slug)):
        return sender
    return master


def sweep(inbox: "InboxStore", slug: str, store: "RedisStore", live_rows: "Callable[[], dict]") -> None:
    """live_rows reads the ledger's tasks at sweep time: a task closed after the tick's own read settles as closed."""
    live = [agent for agent in store.agents(slug) if agent.state != "finished"]
    active = {agent.name for agent in live}
    tasks = {row.get("claimed_by"): row for row in live_rows().values()}
    seats = store.seats.agent_seats(slug)
    gone = [(name, seat) for name, seat in seats if name not in active]
    outcomes = store.seats.exits([name for name, _ in gone])
    pending = gone
    for _ in range(len(gone)):
        quiet = inbox.quiet([name for name, _ in pending])
        due = [(name, seat) for name, seat in pending if not (outcomes[name] and name in quiet)]
        if not due:
            break
        pending = [entry for entry in pending if entry not in due]
        for name, seat in due:
            _settle_gone(inbox, name, seat, tasks.get(name, {}).get("state"), outcomes[name])
    _settle_seat_notices(inbox, {seat for _, seat in seats if seat}, active)
    _settle_peer_mail(inbox, slug, store, active)
    if slug in store.slugs():
        empty = {seat for _, seat in seats} - {agent.seat for agent in live}
        _settle_unfillable(inbox, store.config(slug), empty)


def _settle_gone(inbox: "InboxStore", name: str, seat: str, state: str | None, outcome: dict) -> None:
    if state in ("done", "blocked"):
        exit_text = "finished its task and exited" if state == "done" else "blocked its task and exited"
        settle(inbox, name, "", exit_text)
    elif outcome:
        settle(inbox, name, outcome["seat"], outcome["reason"])
    else:
        settle(inbox, name, seat, "exited")


def _settle_seat_notices(inbox: "InboxStore", seats: set, active: set) -> None:
    """Push stop and wait ended notices left on a seat: closed once the agent that got them has gone. Other mail a
    gone life took is settled by its exit record."""
    from scripts.gates import push_stop
    from scripts.inbox.store import CLOSED
    from scripts.swarm.waits import notice_task

    for seat in sorted(seats):
        for item in inbox.open_items(seat):
            if item.state in CLOSED:
                continue
            task = notice_task(item)
            if not (task or push_stop.is_notice(item)):
                if item.state != "pending":
                    _settle_left_behind(inbox, item, seat, active)
                continue
            owner = _owner(inbox, item, seat)
            if owner in active:
                continue
            if not owner:
                reason = UNKNOWN_OWNER
            elif task:
                reason = f"{owner} left its seat before picking task {task} back up"
            else:
                reason = push_stop.left(owner, "left its seat")
            inbox.close(item.id, BY, "done", reason)


def _settle_left_behind(inbox: "InboxStore", item, seat: str, active: set) -> None:
    taker = _taker(inbox, item)
    if not taker or taker in active or inbox.names.entry(taker).get("type") == "master":
        return
    outcome = inbox.seats.exit_of(taker)
    if outcome:
        _settle_taken(inbox, item, seat, taker, outcome["reason"], bool(outcome["seat"]))


def _settle_peer_mail(inbox: "InboxStore", slug: str, store: "RedisStore", active: set) -> None:
    from scripts.inbox.store import CLOSED

    for agent in store.agents(slug):
        if agent.name not in active or not agent.seat:
            continue
        for item in inbox.open_items(agent.seat):
            if item.state in CLOSED or item.fyi or not item.task or item.task == agent.task:
                continue
            owner = _taker(inbox, item)
            if not owner or owner in active:
                continue
            exit_text = f"left its seat and task {item.task}"
            if inbox.withdraw(item.id, BY, f"cancelled: {owner} {exit_text} before closing it", agent.seat, owner):
                inbox.send(BY, notice_address(inbox, item.sender), _told(item, owner, exit_text), fyi=True)


def _settle_unfillable(inbox: "InboxStore", config, seats: set) -> None:
    for seat in seats:
        why = _no_successor(seat, config)
        if not why:
            continue
        for item in inbox.open_items(seat):
            reason = f"cancelled: {seat} can get no successor: {why}"
            if not inbox.withdraw(item.id, BY, reason, seat):
                continue
            _withdraw_escalations(inbox, item.id, seat)
            if item.sender != BY:
                text = (
                    f"{seat} can get no successor: {why}, so your message {item.id}: "
                    f"{item.text.splitlines()[0][:200]} is closed. "
                    "Send it to whoever carries that work on if it still matters."
                )
                inbox.send(BY, notice_address(inbox, item.sender), text, fyi=True)


def _withdraw_escalations(inbox: "InboxStore", item_id: str, seat: str) -> None:
    from scripts.inbox.wake import TO_MASTER

    for entry in inbox.history(item_id):
        if entry.get("event") == TO_MASTER:
            raised = entry["reason"].rpartition(" ")[2]
            inbox.withdraw(raised, BY, f"cancelled: message {item_id} is closed, {seat} can get no successor")


def _no_successor(seat: str, config) -> str:
    caps = {"eng": config.max_eng, "ci": config.max_ci, "plan": config.max_plan}
    lane, _, slot = seat.partition("@")[0].rpartition("-")
    if lane not in caps or not slot.isdigit():
        return ""
    if config.state == "stopped":
        return "the swarm stopped"
    return f"the {lane} lane cap is {caps[lane]}" if int(slot) > caps[lane] else ""


def _owner(inbox, item, seat):
    """The agent the notice was sent to: the seat's occupant when it was sent."""
    held = [entry["occupant"] for entry in inbox.seats.history(seat) if entry["at"] <= item.created_at]
    return (held[-1:] or [""])[0]


def _told(item, name, exit_text):
    gist = item.text.splitlines()[0][:200] if item.text else ""
    return (
        f"{name} {exit_text} before closing your message {item.id}: {gist}. "
        "It is closed; send it to whoever carries that work on if it still matters."
    )
