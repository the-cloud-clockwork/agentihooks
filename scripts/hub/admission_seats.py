import json
from typing import Any

from scripts.events import factory
from scripts.events.config import BusConfig
from scripts.hive import account_slots


def _used(value: dict[str, Any], hive: str, now: int) -> int:
    holders = sum(
        1
        for row in value["holders"].values()
        if row["hive"] == hive and row["expires_ms"] > now and not row.get("master")
    )
    report = value["hives"].get(hive)
    if report is None or report["expires_ms"] <= now:
        return holders
    return holders + max(report["sessions"] - len(report["observed"]), 0)


def available(hive: str, slots: list[dict[str, Any]] | None, harnesses: tuple[str, ...], now: int) -> bool:
    if not slots:
        return True
    eligible = [slot for slot in slots if slot["harness"] in harnesses and slot["cap"] is not None and slot["cap"] > 0]
    if not eligible:
        return False
    bus = factory.open_bus(BusConfig.from_environ())
    try:
        bucket = bus.state("account-slots")
        for slot in eligible:
            entry = bucket.get(account_slots.key(slot["harness"], slot["account"]))
            if entry is None or _used(json.loads(entry.value), hive, now) < slot["cap"]:
                return True
        return False
    finally:
        bus.close()
