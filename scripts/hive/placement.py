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


@dataclass(frozen=True)
class MasterPlacement:
    hive: str
    account: str
    route_kind: str
    headless: bool


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


def place_master(harness: str, login: str, repo: str, hives: list[dict], now: int) -> MasterPlacement | Wait:
    candidates = []
    for hive in hives:
        if not live(hive, now) or repo not in hive["repos"]:
            continue
        headless = hive["ui"] != YES
        if not headless and MASTER not in hive["roles"]:
            continue
        native = hive["interactive"].get(harness)
        if not headless and native and login in ("", native):
            candidates.append(
                ((False, False), _order(MASTER)(hive), native, MasterPlacement(hive["id"], native, INTERACTIVE, False))
            )
        for slot in hive["slots"]:
            if slot["harness"] != harness or slot["kind"] not in ("subscription", "api"):
                continue
            candidates.append(
                (
                    (headless, True),
                    _order(MASTER)(hive),
                    slot["account"],
                    MasterPlacement(hive["id"], slot["account"], slot["kind"], headless),
                )
            )
    if not candidates:
        return Wait("no live hive can run the master with interactive, subscription or api authentication")
    return min(candidates, key=lambda candidate: candidate[:3])[3]
