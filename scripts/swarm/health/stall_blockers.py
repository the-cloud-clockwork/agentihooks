"""A merge stall and merged tasks left in pr recorded as swarm blockers, so an hour of either opens the master coding
window through the blocker window.

no merge into dev holds while focused pull requests are open, every task in pr when no focus holds, and starts at the
last dev merge. merged task in pr holds while a task sits in pr twenty minutes after its pull request merged and starts
at that mark. The stall-blockers gate picks the mode in the swarm config: observe logs each would-be record to the
swarm gate log, enforce records it in the blocker queue, off skips.
"""

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from scripts.gates import Who, modes
from scripts.gates import log as gate_log
from scripts.swarm import blockers

MERGE_STALL = "no merge into dev"
MERGED_IN_PR = "merged task in pr"
CATEGORIES = (MERGE_STALL, MERGED_IN_PR)
MERGED_MS = 20 * 60_000
MERGE_KEY = "merge-stall"
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

    admitted = freeze.admitted(doc, hierarchy.project(doc)[0])
    return [task for task in tasks if task.get("state") == "pr" and (admitted is None or task["id"] in admitted)]


def merge_stall(
    store: Any, slug: str, doc: dict[str, Any], tasks: list[dict[str, Any]], github: Callable[[str], Any]
) -> Held | None:
    """Return the merge stall when a focused pull request is open and the last dev merge is known."""
    state = json.loads(store.redis.get(store.key(slug, MERGE_KEY)) or "{}")
    if not state.get("merged") or state.get("error"):
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
    """Return the oldest task left in pr twenty minutes after its pull request merged, unless a declared wait holds it."""
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


def _apply(store: Any, slug: str, category: str, found: Held | None, mode: str, now_ms: int) -> list[str]:
    if mode != "enforce":
        if found is None:
            return []
        reason = f"{category}: {found.detail}"
        gate_log.append(slug, gate_log.Row.of(GATE.name, "observe", Who(name=SENDER), reason=reason, now_ms=now_ms))
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
    """Record, log or skip both categories by the stall-blockers gate mode, clearing a category that stopped holding."""
    mode = str(modes.configured(GATE, store.config(slug).gates))
    if mode == "off":
        return []
    rows = list(tasks)
    stall = merge_stall(store, slug, doc, rows, github)
    left = merged_in_pr(rows, github, held, now_ms)
    return _apply(store, slug, MERGE_STALL, stall, mode, now_ms) + _apply(store, slug, MERGED_IN_PR, left, mode, now_ms)
