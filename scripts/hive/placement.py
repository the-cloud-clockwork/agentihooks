"""Pure placement: pick the hive that launches an agent, or say why it waits."""

import math
from collections.abc import Callable
from dataclasses import dataclass

from scripts.hive.registry import UI_ROLES, live

MASTER = "master"
INTERACTIVE = "interactive"
YES = "yes"
NO_UI_HIVE = "no UI hive live"
HIVE = "hive"
HEADLESS = "headless hive"
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


@dataclass(frozen=True)
class Choice:
    hive: str
    fallback: str = ""


def hosts(role: str, hive: dict) -> bool:
    return role in hive["roles"] and (role not in UI_ROLES or hive["ui"] == YES)


def _room(hive: dict) -> int:
    return hive["max_agents"] - hive["agents"]


def _checks(
    role: str, harness: str, slot: str, route_kind: str, repo: str, kind: str = HIVE
) -> list[tuple[Callable[[dict], bool], str]]:
    return [
        (lambda hive: role == MASTER or _room(hive) > 0, f"no {kind} for {role} has room"),
        (
            lambda hive: any(item["harness"] == harness and item["account"] == slot for item in hive["slots"]),
            f"no {kind} for {role} advertises the {harness} slot {slot}",
        ),
        (lambda hive: repo in hive["repos"], f"no {kind} for {role} lists the repo {repo}"),
        (
            lambda hive: route_kind != INTERACTIVE or hive["interactive"].get(harness) == slot,
            f"no {kind} for {role} holds the {harness} login for {slot}",
        ),
    ]


def _order(role: str) -> Callable[[dict], tuple]:
    return lambda hive: (
        hive["prefer"].get(role, UNRANKED),
        -_room(hive),
        hive["ephemeral"] == YES,
        hive["id"],
    )


def _pick(
    role: str, harness: str, slot: str, route_kind: str, repo: str, candidates: list[dict], kind: str = HIVE
) -> Choice | Wait:
    for check, reason in _checks(role, harness, slot, route_kind, repo, kind):
        candidates = [hive for hive in candidates if check(hive)]
        if not candidates:
            return Wait(reason)
    return Choice(min(candidates, key=_order(role))["id"])


def choose(
    role: str, harness: str, slot: str, route_kind: str, repo: str, hives: list[dict], now: int
) -> Choice | Wait:
    alive = [hive for hive in hives if live(hive, now)]
    candidates = [hive for hive in alive if role in hive["roles"]]
    ui_live = any(hive["ui"] == YES for hive in alive)
    if not candidates:
        return Wait(NO_UI_HIVE if role in UI_ROLES and not ui_live else f"no live hive takes {role}")
    ui = [hive for hive in candidates if hive["ui"] == YES]
    headless = [hive for hive in candidates if hive["ui"] != YES]
    if role in UI_ROLES:
        pool = [hive for hive in candidates if hosts(role, hive)]
        return _pick(role, harness, slot, route_kind, repo, pool) if pool else Wait(NO_UI_HIVE)
    if not ui:
        return _pick(role, harness, slot, route_kind, repo, headless)
    picked = (
        _pick(role, harness, slot, route_kind, repo, headless, HEADLESS)
        if headless
        else Wait(f"no live {HEADLESS} takes {role}")
    )
    if isinstance(picked, Choice):
        return picked
    fallback = _pick(role, harness, slot, route_kind, repo, ui)
    return Choice(fallback.hive, picked.reason) if isinstance(fallback, Choice) else picked


def place(role: str, harness: str, slot: str, route_kind: str, repo: str, hives: list[dict], now: int) -> str | Wait:
    chosen = choose(role, harness, slot, route_kind, repo, hives, now)
    return chosen.hive if isinstance(chosen, Choice) else chosen


def place_master(harness: str, login: str, repo: str, hives: list[dict], now: int) -> MasterPlacement | Wait:
    candidates = []
    for hive in hives:
        if not live(hive, now) or repo not in hive["repos"] or not hosts(MASTER, hive):
            continue
        native = hive["interactive"].get(harness)
        if native and login in ("", native):
            candidates.append(
                (False, _order(MASTER)(hive), native, MasterPlacement(hive["id"], native, INTERACTIVE, False))
            )
        for slot in hive["slots"]:
            if slot["harness"] != harness or slot["kind"] not in ("subscription", "api"):
                continue
            candidates.append(
                (
                    True,
                    _order(MASTER)(hive),
                    slot["account"],
                    MasterPlacement(hive["id"], slot["account"], slot["kind"], False),
                )
            )
    if not candidates:
        return Wait("no live hive can run the master with interactive, subscription or api authentication")
    return min(candidates, key=lambda candidate: candidate[:3])[3]
