from typing import Protocol

MASTER = "master"


class Seat(Protocol):
    seat: str


def force(seat: Seat) -> str:
    # ruleid: quick-master-force-return
    if seat.seat == MASTER:
        return "master"
    return "started"


def force_successor(seat: Seat) -> str:
    # ok: quick-master-force-return
    if seat.seat == MASTER:
        return "started"
    return "started"
