"""Pure placement: pick the hive that launches an agent, or say why it waits."""

import math
from collections.abc import Callable
from dataclasses import dataclass

from scripts.hive.registry import UI_ROLES, live

MASTER = "master"
INTERACTIVE = "interactive"
YES = "yes"
NO_UI_HIVE = "no UI hive live"
UNRANKED = math.inf


@dataclass(frozen=True)
class Wait:
    reason: str


def _room(hive: dict) -> int:
    return hive["max_agents"] - hive["agents"]


def _checks(role: str, harness: str, slot: str, route_kind: str, repo: str) -> list[tuple[Callable[[dict], bool], str]]:
    return [
        (lambda hive: role == MASTER or _room(hive) > 0, f"no hive for {role} has room"),
        (
            lambda hive: any(item["harness"] == harness and item["account"] == slot for item in hive["slots"]),
            f"no hive for {role} advertises the {harness} slot {slot}",
        ),
        (lambda hive: repo in hive["repos"], f"no hive for {role} lists the repo {repo}"),
        (
            lambda hive: route_kind != INTERACTIVE or hive["interactive"].get(harness) == slot,
            f"no hive for {role} holds the {harness} login for {slot}",
        ),
    ]


def _order(role: str) -> Callable[[dict], tuple]:
    return lambda hive: (
        hive["prefer"].get(role, UNRANKED),
        -_room(hive),
        hive["ephemeral"] == YES,
        hive["id"],
    )


def place(role: str, harness: str, slot: str, route_kind: str, repo: str, hives: list[dict], now: int) -> str | Wait:
    alive = [hive for hive in hives if live(hive, now)]
    candidates = [hive for hive in alive if role in hive["roles"]]
    ui_live = any(hive["ui"] == YES for hive in alive)
    if not candidates:
        if role == MASTER and alive and not ui_live:
            candidates = alive
        elif role in UI_ROLES and not ui_live:
            return Wait(NO_UI_HIVE)
        else:
            return Wait(f"no live hive takes {role}")
    for check, reason in _checks(role, harness, slot, route_kind, repo):
        candidates = [hive for hive in candidates if check(hive)]
        if not candidates:
            return Wait(reason)
    return min(candidates, key=_order(role))["id"]
