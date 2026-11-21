from typing import Any


def _order(inbox: Any, ordered: dict[str, str], remaining: list[str], text: str, hive: str) -> None:
    return None


def _withdraw(inbox: Any, ordered: dict[str, str], reason: str) -> None:
    return None


def drain(inbox: Any, remaining: list[str], hive: str) -> int:
    ordered: dict[str, str] = {}
    while remaining:
        # ruleid: hive-order-never-withdrawn
        _order(inbox, ordered, remaining, "hand off", hive)
        remaining = remaining[1:]
    return len(ordered)


def withdraws_another_map(inbox: Any, remaining: list[str], hive: str) -> int:
    ordered: dict[str, str] = {}
    while remaining:
        # ruleid: hive-order-never-withdrawn
        _order(inbox, ordered, remaining, "hand off", hive)
        remaining = remaining[1:]
    _withdraw(inbox, {}, f"hive {hive} was removed")
    return len(ordered)


def settle(inbox: Any, remaining: list[str], hive: str) -> int:
    ordered: dict[str, str] = {}
    while remaining:
        # ok: hive-order-never-withdrawn
        _order(inbox, ordered, remaining, "hand off", hive)
        remaining = remaining[1:]
    _withdraw(inbox, ordered, f"hive {hive} drained")
    return len(ordered)
