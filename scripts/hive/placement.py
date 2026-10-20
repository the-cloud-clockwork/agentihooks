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
UI_HIVE = "UI hive"
LOCAL_MACHINE = "local machine"
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


def hosts(role: str, hive: dict, aliases: tuple[str, ...] = ()) -> bool:
    return not {role, *aliases} & set(hive["excluded"])


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
    candidates = [hive for hive in hives if live(hive, now) and hosts(role, hive)]
    if not candidates:
        return Wait(f"no live hive takes {role}")
    ui_first = role in UI_ROLES
    first = [hive for hive in candidates if (hive["ui"] == YES) == ui_first]
    rest = [hive for hive in candidates if (hive["ui"] == YES) != ui_first]
    if not rest:
        return _pick(role, harness, slot, route_kind, repo, first)
    kind = UI_HIVE if ui_first else HEADLESS
    picked = (
        _pick(role, harness, slot, route_kind, repo, first, kind) if first else Wait(f"no live {kind} takes {role}")
    )
    if isinstance(picked, Choice):
        return picked
    fallback = _pick(role, harness, slot, route_kind, repo, rest)
    return Choice(fallback.hive, picked.reason) if isinstance(fallback, Choice) else picked


def place(role: str, harness: str, slot: str, route_kind: str, repo: str, hives: list[dict], now: int) -> str | Wait:
    chosen = choose(role, harness, slot, route_kind, repo, hives, now)
    return chosen.hive if isinstance(chosen, Choice) else chosen


def overflow(role: str, waited: Wait, local_agents: int, local_max: int) -> Choice | Wait:
    """A worker no hive takes runs on the local machine while that machine runs fewer agents than its own cap."""
    if role in UI_ROLES:
        return waited
    if local_agents >= local_max:
        return Wait(f"{waited.reason}, and the local machine runs {local_agents} of its {local_max} agents")
    return Choice(LOCAL_MACHINE, waited.reason)


def place_master(harness: str, login: str, repo: str, hives: list[dict], now: int) -> MasterPlacement | Wait:
    candidates = []
    for hive in hives:
        if not live(hive, now) or repo not in hive["repos"] or not hosts(MASTER, hive):
            continue
        headless = hive["ui"] != YES
        native = hive["interactive"].get(harness)
        if native and login in ("", native):
            candidates.append(
                (
                    headless,
                    False,
                    _order(MASTER)(hive),
                    native,
                    MasterPlacement(hive["id"], native, INTERACTIVE, False),
                )
            )
        for slot in hive["slots"]:
            if slot["harness"] != harness or slot["kind"] not in ("subscription", "api"):
                continue
            candidates.append(
                (
                    headless,
                    True,
                    _order(MASTER)(hive),
                    slot["account"],
                    MasterPlacement(hive["id"], slot["account"], slot["kind"], False),
                )
            )
    if not candidates:
        return Wait("no live hive can run the master with interactive, subscription or api authentication")
    return min(candidates, key=lambda candidate: candidate[:4])[4]
