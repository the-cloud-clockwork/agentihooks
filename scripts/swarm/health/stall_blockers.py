"""A merge stall and merged tasks left in pr recorded as swarm blockers, so an hour of either opens the master coding
window through the blocker window.

no merge into dev holds while focused pull requests are open, every task in pr when no focus holds, and starts at the
later of the last dev merge and the first pass that saw it hold. merged task in pr holds while a task sits in pr
twenty minutes after its pull request merged and starts at that mark. The stall-blockers gate picks the mode in the
swarm config: observe logs each would-be record once per start to the swarm gate log, enforce records it in the
blocker queue, off skips.
"""

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from typing import Any

from scripts.gates import Who, modes
from scripts.gates import log as gate_log
from scripts.swarm import blockers
from scripts.swarm.health import merge_stall as merge_stall_observation

MERGE_STALL = "no merge into dev"
MERGED_IN_PR = "merged task in pr"
CATEGORIES = (MERGE_STALL, MERGED_IN_PR)
MERGED_MS = 20 * 60_000
MERGE_KEY = "merge-stall"
KEY = "stall-blockers"
STALE_MS = 2 * merge_stall_observation.CACHE_MS
SENDER = "swarm"


@dataclass(frozen=True)
class StallBlockersGate:
    name: str = "stall-blockers"
    default_mode: str = "observe"


GATE = StallBlockersGate()


@dataclass(frozen=True)
class Held:
    category: str
    since: int
    detail: str


def _focused(doc: dict[str, Any], tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the tasks in pr inside the focus, or every task in pr when no focus has open work."""
    from scripts.swarm import freeze
    from scripts.swarm_ledger.repository import hierarchy

    admitted = freeze.admitted(doc, hierarchy.project(doc)[0])  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    return [task for task in tasks if task.get("state") == "pr" and (admitted is None or task["id"] in admitted)]


def merge_stall(
    store: Any, slug: str, doc: dict[str, Any], tasks: list[dict[str, Any]], github: Callable[[str], Any], now_ms: int
) -> Held | None:
    """Return the merge stall when a focused pull request is open and a fresh merge observation knows the last dev
    merge."""
    state = json.loads(store.redis.get(store.key(slug, MERGE_KEY)) or "{}")
    if not state.get("merged") or state.get("error") or now_ms - state.get("tried", 0) > STALE_MS:
        return None
    open_urls = [
        task["pr_url"]
        for task in _focused(doc, tasks)
        if task.get("pr_url") and (found := github(task["pr_url"])) is not None and found.state == "OPEN"
    ]
    if not open_urls:
        return None
    detail = f"{len(open_urls)} focused pull requests open and no merge into dev on {state.get('repo', 'dev')}"
    return Held(MERGE_STALL, int(state["merged"]), detail)


def merged_in_pr(
    tasks: list[dict[str, Any]], github: Callable[[str], Any], held: Callable[[dict[str, Any]], bool], now_ms: int
) -> Held | None:
    """Return the oldest task left in pr twenty minutes after its pull request merged, unless a declared wait holds
    it."""
    left: list[tuple[int, str]] = []
    for task in tasks:
        url = task.get("pr_url")
        if task.get("state") != "pr" or not url:
            continue
        found = github(url)
        if found is None or found.state != "MERGED" or not found.merged_at or held(task):
            continue
        if now_ms - found.merged_at >= MERGED_MS:
            left.append((found.merged_at + MERGED_MS, str(task.get("title", task["id"]))))
    if not left:
        return None
    since, title = min(left)
    return Held(MERGED_IN_PR, since, f"{len(left)} tasks still in pr after their pull request merged, oldest {title}")


def _apply(
    store: Any, slug: str, category: str, found: Held | None, mode: str, now_ms: int, logged: dict[str, int]
) -> list[str]:
    if mode != "enforce":
        if found is None or logged.get(category) == found.since:
            if found is None:
                logged.pop(category, None)
            return []
        logged[category] = found.since
        reason = f"{category}: {found.detail}"
        row = gate_log.Row.of(GATE.name, "observe", Who(name=SENDER), reason=reason, now_ms=now_ms)  # pyright: ignore[reportUnknownMemberType]
        gate_log.append(slug, row)  # pyright: ignore[reportUnknownMemberType]
        return [f"stall blockers: logged {reason}"]
    current = blockers.active(store, slug, now_ms).get(category)
    if current is not None and (found is None or current.first_seen < found.since):
        blockers.clear(store, slug, category)
    if found is None:
        return [f"stall blockers: cleared {category}"] if current is not None else []
    since = max(found.since, now_ms - blockers.TTL_MS // 2)
    blockers.record(store, slug, category, found.detail, now_ms, since=since)
    return []


def observe(
    store: Any,
    slug: str,
    doc: dict[str, Any],
    tasks: Iterable[dict[str, Any]],
    github: Callable[[str], Any],
    now_ms: int,
    held: Callable[[dict[str, Any]], bool],
) -> list[str]:
    """Record, log once per start or skip both categories by the stall-blockers gate mode, clearing a category that
    stopped holding; the stall starts at the later of the last dev merge and the first pass that saw it hold. A ledger
    with no task in pr costs no read: its categories stop recurring and fall out of the sustained set, and state left
    unobserved past STALE_MS starts over."""
    rows = list(tasks)
    if not any(task.get("state") == "pr" for task in rows):
        return []
    mode = str(modes.configured(GATE, store.config(slug).gates))  # pyright: ignore[reportUnknownMemberType]
    if mode == "off":
        return []
    key = store.key(slug, KEY)
    state = json.loads(store.redis.get(key) or "{}")
    if now_ms - state.get("at", now_ms) > STALE_MS:
        state = {}
    state["at"] = now_ms
    stall = merge_stall(store, slug, doc, rows, github, now_ms)
    state["open_since"] = (state.get("open_since") or now_ms) if stall else None
    if stall:
        stall = replace(stall, since=max(stall.since, state["open_since"]))
    left = merged_in_pr(rows, github, held, now_ms)
    logged = state.setdefault("logged", {})
    lines = _apply(store, slug, MERGE_STALL, stall, mode, now_ms, logged)
    lines += _apply(store, slug, MERGED_IN_PR, left, mode, now_ms, logged)
    store.redis.set(key, json.dumps(state), px=blockers.TTL_MS)
    return lines
