"""Typed links between the seats of a swarm: `delegates-to` allows a send, `can-observe` allows reading only.

A link names a seat (`eng-1`) or a lane (`eng`) at each end. A swarm without links, the operator, the master
and a sender that holds no seat are never restricted.
"""

from scripts.inbox.seats import is_seat
from scripts.inbox.store import InboxError

DELEGATES = "delegates-to"
OBSERVES = "can-observe"
KINDS = (DELEGATES, OBSERVES)
MASTER = "master"


def check_send(inbox, sender, address):
    found = _between(inbox, sender, address)
    if found is None or DELEGATES in found[2]:
        return
    source, target, kinds = found
    if OBSERVES in kinds:
        raise InboxError(
            f"{source} can only observe {target}: it may read that seat's items and status, not send to it"
        )
    raise InboxError(f"no delegates-to link from {source} to {target} in the swarm's links; the send is refused")


def check_observe(inbox, reader, address):
    found = _between(inbox, reader, address)
    if found is not None and not found[2]:
        raise InboxError(f"no link from {found[0]} to {found[1]} in the swarm's links; reading its items is refused")


def _between(inbox, sender, address):
    """(sender seat, target seat, link kinds between them), or None when no link rule applies."""
    from scripts.swarm.store import RedisStore, SwarmError

    source = _seat(inbox, sender)
    target = _seat(inbox, address)
    if not (source and target) or MASTER in (_name(source), _name(target)):
        return None
    slug = source.split("@", 1)[1]
    if target.split("@", 1)[1] != slug:
        return None
    try:
        links = RedisStore(inbox.redis).config(slug).links
    except SwarmError:
        return None
    if not links:
        return None
    kinds = {link["kind"] for link in links if _ends(link["from"], source) and _ends(link["to"], target)}
    return source, target, kinds


def _seat(inbox, name):
    return name if is_seat(name) else inbox.seats.seat_of(name)


def _name(seat):
    return seat.split("@", 1)[0]


def _ends(end, seat):
    return end in (_name(seat), _name(seat).split("-", 1)[0])
