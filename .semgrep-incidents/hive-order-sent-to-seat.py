from typing import Any


def to_seat_or_name(inbox: Any, agent: Any, text: str, hive: str, slug: str) -> None:
    # ruleid: hive-order-sent-to-seat
    inbox.send("operator", agent.seat or agent.name, text.format(hive=hive, slug=slug))


def to_seat(inbox: Any, agent: Any, text: str, hive: str, slug: str) -> None:
    # ruleid: hive-order-sent-to-seat
    inbox.send("operator", agent.seat, text.format(slug=slug, hive=hive))


def to_name(inbox: Any, agent: Any, text: str, hive: str, slug: str) -> None:
    # ok: hive-order-sent-to-seat
    inbox.send("operator", agent.name, text.format(hive=hive, slug=slug))


def seat_notice(inbox: Any, agent: Any, text: str, slug: str) -> None:
    # ok: hive-order-sent-to-seat
    inbox.send("swarm", agent.seat, text.format(slug=slug))
