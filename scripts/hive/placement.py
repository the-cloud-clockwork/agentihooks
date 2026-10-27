"""Pure placement: pick the hive that launches an agent, or say why it waits."""

import math
from collections.abc import Callable
from dataclasses import dataclass, field

from scripts.hive.registry import UI_ROLES, live
from scripts.master_claude_login import ROUTE as MASTER_LOGIN

MASTER = "master"
INTERACTIVE = "interactive"
YES = "yes"
NO_UI_HIVE = "no UI hive live"
HIVE = "hive"
HEADLESS = "headless hive"
UI_HIVE = "UI hive"
LOCAL_MACHINE = "local machine"
LOCAL_FULL = ", and the local machine runs "
HAS_ROOM = "has room"
NEEDS_WORKSTATION = "the task needs the workstation, so no remote hive takes it"
NEEDS_CAPABILITY = "the task needs a hive with {needs}, and no hive declares it"
NEEDS_TOGETHER = "the task needs a hive with {needs}, and no single hive declares all of them"
UNRANKED = math.inf


@dataclass(frozen=True)
class Wait:
    reason: str
    role: str = field(default="", compare=False)


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
    return hive.get("draining") != YES and not {role, *aliases} & set(hive["excluded"])


def equipped(task: dict, hive: dict | None) -> bool:
    return set(task.get("needs") or ()) <= set((hive or {}).get("capabilities") or ())


def needs_refusal(task: dict, hives: list[dict]) -> str:
    declared = {name for hive in hives for name in hive.get("capabilities") or ()}
    needs = task.get("needs") or ()
    missing = [name for name in needs if name not in declared]
    return (
        NEEDS_CAPABILITY.format(needs=", ".join(missing)) if missing else NEEDS_TOGETHER.format(needs=", ".join(needs))
    )


def for_swarm(hives: list[dict], slug: str) -> list[dict]:
    return [
        {**hive, "excluded": [role for role in hive["excluded"] if role != MASTER]}
        if slug in hive.get("master_swarms", ())
        else hive
        for hive in hives
    ]


def _room(hive: dict) -> int:
    return hive["room"]


def _refused(reason: str, hives: list[dict]) -> str:
    held = "; ".join(f"{hive['id']} {hive['refusal']}" for hive in hives if hive.get("refusal"))
    return f"{reason}: {held}" if held else reason


def _checks(
    role: str, harness: str, slot: str, route_kind: str, repo: str, kind: str = HIVE
) -> list[tuple[Callable[[dict], bool], str]]:
    return [
        (lambda hive: role == MASTER or _room(hive) > 0, f"no {kind} for {role} {HAS_ROOM}"),
        (
            lambda hive: any(item["harness"] == harness and item["account"] == slot for item in hive["slots"]),
            f"no {kind} for {role} advertises the {harness} slot {slot}",
        ),
        (lambda hive: repo in hive["repos"], f"no {kind} for {role} lists the repo {repo}"),
        (
            lambda hive: route_kind != INTERACTIVE or hive["interactive"].get(harness) == slot,
            f"no {kind} for {role} holds the {harness} login for {slot}",
        ),
        (
            lambda hive: route_kind != MASTER_LOGIN or hive["master_login"].get(harness) == slot,
            f"no {kind} for {role} holds the {harness} master login for {slot}",
        ),
    ]


def _order(role: str) -> Callable[[dict], tuple]:
    return lambda hive: (
        hive["prefer"].get(role, UNRANKED),
        hive["agents"] / max(hive["max_agents"], 1),
        hive["ephemeral"] == YES,
        hive["id"],
    )


def _pick(
    role: str, harness: str, slot: str, route_kind: str, repo: str, candidates: list[dict], kind: str = HIVE
) -> Choice | Wait:
    for index, (check, reason) in enumerate(_checks(role, harness, slot, route_kind, repo, kind)):
        kept = [hive for hive in candidates if check(hive)]
        if not kept:
            return Wait(_refused(reason, candidates) if index == 0 else reason)
        candidates = kept
    return Choice(min(candidates, key=_order(role))["id"])


def choose(
    role: str, harness: str, slot: str, route_kind: str, repo: str, hives: list[dict], now: int
) -> Choice | Wait:
    candidates = [hive for hive in hives if live(hive, now) and hosts(role, hive)]
    if not candidates:
        return Wait(f"no live hive takes {role}")
    ui_first = role in UI_ROLES
    if not ui_first:
        return _pick(role, harness, slot, route_kind, repo, candidates)
    first = [hive for hive in candidates if (hive["ui"] == YES) == ui_first]
    rest = [hive for hive in candidates if (hive["ui"] == YES) != ui_first]
    if not rest:
        return _pick(role, harness, slot, route_kind, repo, first)
    kind = UI_HIVE if ui_first else HEADLESS
    picked = (
        _pick(role, harness, slot, route_kind, repo, first, kind) if first else Wait(f"no live {kind} takes {role}")
    )
    if isinstance(picked, Choice) or role != MASTER:
        return picked
    fallback = _pick(role, harness, slot, route_kind, repo, rest)
    return Choice(fallback.hive, picked.reason) if isinstance(fallback, Choice) else picked


def place(role: str, harness: str, slot: str, route_kind: str, repo: str, hives: list[dict], now: int) -> str | Wait:
    chosen = choose(role, harness, slot, route_kind, repo, hives, now)
    return chosen.hive if isinstance(chosen, Choice) else chosen


def overflow(role: str, waited: Wait, local_agents: int, local_max: int, reserved: int = 0) -> Choice | Wait:
    if role in UI_ROLES:
        return waited
    if local_agents + reserved >= local_max:
        return local_full(waited, local_agents, local_max, reserved)
    return Choice(LOCAL_MACHINE, waited.reason)


def local_full(waited: Wait, local_agents: int, local_max: int, reserved: int) -> Wait:
    kept = f" with {reserved} kept for waiting workstation tasks" if reserved else ""
    return Wait(f"{waited.reason}{LOCAL_FULL}{local_agents} of its {local_max} agents{kept}")


def _private_master(hive: dict, harness: str, front: bool) -> bool:
    return front and (
        bool(hive["master_login"].get(harness)) or (harness == "claude" and hive.get("master_login_enabled") == YES)
    )


def place_master(
    harness: str, login: str, repo: str, hives: list[dict], now: int, front: bool = False
) -> MasterPlacement | Wait:
    hosting = [hive for hive in hives if live(hive, now) and hosts(MASTER, hive)]
    if not hosting:
        return Wait(f"no live hive takes {MASTER}")
    listing = [hive for hive in hosting if repo in hive["repos"]]
    if not listing:
        return Wait(f"no {HIVE} for {MASTER} lists the repo {repo}")
    candidates = []
    for hive in listing:
        headless = hive["ui"] != YES
        account = hive["master_login"].get(harness, "")
        forced = _private_master(hive, harness, front)
        if forced:
            if login in ("", account):
                placed = MasterPlacement(hive["id"], account, MASTER_LOGIN, False)
                candidates.append((headless, not bool(account), _order(MASTER)(hive), account, placed))
            continue
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
    if not candidates and login:
        return Wait(f"no {HIVE} for {MASTER} holds the {harness} login for {login}")
    if not candidates:
        return Wait(f"no {HIVE} for {MASTER} advertises a {harness} slot")
    return min(candidates, key=lambda candidate: candidate[:4])[4]
