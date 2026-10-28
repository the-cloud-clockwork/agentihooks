"""Local slot claims that left a live local pane out of their count: each claim's journalled names, compared with the
agents of every swarm that held a pane on the claiming machine at that instant."""

import re

from scripts.swarm.health.findings import Finding
from scripts.swarm.store import SwarmError

KIND = "local slot census"
UNCLAIMED = "unclaimed local launch"
CLAIM = "local slot claim"
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


def _dropped(lives, at, slug, claim):
    counted = {name for name in claim["names"].split(", ") if name}
    dropped = {}
    for life in lives:
        if _alive(life, at, slug, claim) and not {life["name"], f"{life['slug']}/{life['name']}"} & counted:
            dropped.setdefault((life["slug"], life["name"]), life)
    return [dropped[key] for key in sorted(dropped)]


def findings(record: dict) -> list[Finding]:
    """The record is what spawn_read.slot_claims reads: claim lines of the tick journal and every swarm's lives."""
    if record["claims"] is None:
        raise SwarmError(f"local slot claims unavailable: {record['journal_error']}")
    prefix = f"{record['slug']}: "
    found = []
    for line in record["claims"]:
        message = line["message"]
        if not message.startswith(prefix) or not (claim := LINE.fullmatch(message[len(prefix) :])):
            continue
        dropped = _dropped(record["lives"], line["at"], record["slug"], claim)
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


def _launched(life, slug, machine, since_ms):
    return (
        life["slug"] == slug
        and life.get("runtime_backend", "local") == "local"
        and life.get("hive", "") in ("", machine)
        and life.get("lane") != "master"
        and bool(life.get("pane_id"))
        and life.get("started_at", 0) >= since_ms
    )


def unclaimed(record: dict, machine: str, since_ms: int) -> list[Finding]:
    """Local launches on `machine` of the watched swarm since `since_ms` whose name no claim line of that swarm carries."""
    if record["claims"] is None:
        raise SwarmError(f"local slot claims unavailable: {record['journal_error']}")
    slug = record["slug"]
    prefix = f"{slug}: "
    claimed = {
        claim["name"]
        for line in record["claims"]
        if line["message"].startswith(prefix) and (claim := LINE.fullmatch(line["message"][len(prefix) :]))
    }
    missing = sorted(
        (life for life in record["lives"] if _launched(life, slug, machine, since_ms) and life["name"] not in claimed),
        key=lambda life: life["name"],
    )
    if not missing:
        return []
    names = ", ".join(life["name"] for life in missing)
    return [
        Finding(
            UNCLAIMED,
            slug,
            f"{len(missing)} local launches of {slug} in the last hour held a pane with no local slot claim: {names}",
            tuple(
                f"{life['name']} on task {life.get('task', '')} started at {life.get('started_at', 0)} "
                f"on {life.get('hive') or 'the workstation'}"
                for life in missing
            ),
            "a local launch with a pane and no local slot claim journal line",
            len(missing),
        )
    ]
