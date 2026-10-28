"""Local slot claims that left a live local pane out of their count: each claim's journalled names, compared with the
agents of every swarm that held a pane on the claiming machine at that instant."""

import re

from scripts.swarm.health.findings import Finding
from scripts.swarm.store import SwarmError

KIND = "local slot census"
CLAIM = "local slot claim"
SPAWN_WINDOW_MS = 60_000
LINE = re.compile(
    r"local slot claim (?P<name>\S+) for (?P<task>\S+) on (?P<machine>\S+) counted \d+ of \d+: "
    r"(?P<names>.*)",
    re.S,
)


def _alive(life, at, slug, claim):
    return (
        life.get("runtime_backend", "local") == "local"
        and life.get("hive", "") in ("", claim["machine"])
        and life.get("lane") != "master"
        and life.get("state") not in ("finished", "ended")
        and bool(life.get("pane_id"))
        and life.get("started_at", 0) <= at
        and not 0 < life.get("ended_at", 0) <= at
        and not (life["slug"] == slug and life.get("task") == claim["task"])
    )


def _dropped(lives, at, slug, claim, own, now):
    counted = {name for name in claim["names"].split(", ") if name}
    dropped = {}
    for life in lives:
        if (
            _alive(life, at, slug, claim)
            and not {life["name"], f"{life['slug']}/{life['name']}"} & counted
            and not _racing(life, at, own, now)
        ):
            dropped.setdefault((life["slug"], life["name"]), life)
    return [dropped[key] for key in sorted(dropped)]


def _racing(life, at, own, now):
    if any(at < claimed <= at + SPAWN_WINDOW_MS for claimed in own.get((life["slug"], life["name"]), ())):
        return True
    return now < at + SPAWN_WINDOW_MS and life.get("started_at", 0) > at - SPAWN_WINDOW_MS


def _own_claims(claims):
    own = {}
    for line in claims:
        swarm, _, rest = line["message"].partition(": ")
        if claim := LINE.fullmatch(rest):
            own.setdefault((swarm, claim["name"]), []).append(line["at"])
    return own


def findings(record: dict) -> list[Finding]:
    """The record is what spawn_read.slot_claims reads: claim lines of the tick journal and every swarm's lives."""
    if record["claims"] is None:
        raise SwarmError(f"local slot claims unavailable: {record['journal_error']}")
    prefix = f"{record['slug']}: "
    own = _own_claims(record["claims"])
    found = []
    for line in record["claims"]:
        message = line["message"]
        if not message.startswith(prefix) or not (claim := LINE.fullmatch(message[len(prefix) :])):
            continue
        dropped = _dropped(record["lives"], line["at"], record["slug"], claim, own, record["now"])
        if not dropped:
            continue
        names = ", ".join(life["name"] for life in dropped)
        found.append(
            Finding(
                KIND,
                f"{claim['name']}@{line['at']}",
                f"the local slot claim for {claim['name']} left {len(dropped)} live local agents out: {names}",
                (
                    f"journal at {line['at']} pid {line['pid']}: {message}",
                    *(f"{life['name']} of {life['slug']} on task {life.get('task', '')}" for life in dropped),
                ),
                "a live local pane missing from a local slot claim's count",
                len(dropped),
            )
        )
    return found
