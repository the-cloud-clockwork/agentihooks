"""Failed master launches of a watched swarm: one finding per outage, read from the master seat's transfer rows, its
restore outcomes and the tick journal, and a replay counting the failures no detector put before the Doctor master."""

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from hooks.secrets import redact
from scripts.doctor import spawns
from scripts.swarm.health.findings import Finding
from scripts.swarm.store import MASTER, SwarmError

KIND = "master launch failed"
OLD_ID = "failed-spawn/master"
MISSED = "master-launch-missed"
MATCH_MS = 60_000
JOURNAL_MS = 3_600_000
ERROR_CHARS = 300
FAILED = re.compile(r"master spawn failed: (.+)")
AWAITING = "awaiting-decision"
PATHS = {"recycle": "recycle handoff", "restore": "native resume"}


class Unavailable(SwarmError):
    pass


@dataclass(frozen=True)
class Attempt:
    at: int
    path: str
    identity: str
    error: str


def sanitize(text):
    return " ".join(redact(text, mode="strict").split())[:ERROR_CHARS]


def _iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _lines(record):
    prefix = f"{record['slug']}: "
    return [
        (line["at"], line["pid"], match[1])
        for line in record["journal"] or []
        if line["message"].startswith(prefix) and (match := FAILED.fullmatch(line["message"][len(prefix) :]))
    ]


def _retried(rows):
    return {row["retry_of"]: row["at"] for row in rows if row.get("retry_of")}


def _failed_at(row, retried):
    return retried.get(row["id"]) or row["binding"].get("at") or row["at"]


def _take(lines, at, record):
    if record["journal"] is None:
        return f"unavailable: {record['journal_error']}"
    match = next((line for line in lines if at <= line[0] <= at + MATCH_MS), None)
    if match is None:
        return "unavailable: no master spawn failed line in the journal within a minute of the failure"
    lines.remove(match)
    return match[2]


def attempts(record):
    """Every failed master launch, oldest first, each with its error or why the error is unavailable."""
    lines, retried = sorted(_lines(record)), _retried(record["transfers"])
    resumes = {o["transfer"]: o for o in record["restored"] if o["lane"] == MASTER and o["outcome"] == AWAITING}
    failed = sorted(
        ((_failed_at(row, retried), row) for row in record["transfers"] if row["binding"]["state"] == "absent"),
        key=lambda pair: pair[0],
    )
    found = []
    for at, row in failed:
        outcome = resumes.pop(row["id"], None)
        error = outcome["reason"] if outcome else _take(lines, at, record)
        path = PATHS.get(row["reason"], row["reason"])
        found.append(Attempt(at, path, f"transfer {row['id']} for {row['successor']}", sanitize(error)))
    found += [
        Attempt(o["at"], PATHS["restore"], f"restore of {o['name']} on transfer {o['transfer']}", sanitize(o["reason"]))
        for o in resumes.values()
    ]
    found += [Attempt(at, "fresh", f"tick {pid} at {_iso(at)}", sanitize(error)) for at, pid, error in lines]
    return sorted(found, key=lambda a: a.at)


def _bound(record):
    return [
        *(row["binding"]["at"] for row in record["transfers"] if row["binding"]["state"] == "live"),
        *(a["started_at"] for a in record["agents"] if a["lane"] == MASTER and a["state"] == "working"),
    ]


def findings(record):
    last_bound = max(_bound(record), default=-1)
    failed = [a for a in attempts(record) if a.at > last_bound]
    if not failed:
        if record["journal"] is None:
            raise Unavailable(f"fresh master launch failures unavailable: {record['journal_error']}")
        return []
    first, last = failed[0], failed[-1]
    evidence = (
        f"error: {last.error}",
        f"attempt: {last.identity}, {last.path} at {_iso(last.at)}",
        f"binding: no master bound since {_iso(first.at)}",
    )
    if record["journal"] is None:
        evidence += (f"fresh launch failures unavailable: {record['journal_error']}",)
    return [
        Finding(
            KIND,
            f"{record['slug']}@{first.at}",
            f"{len(failed)} failed master launches with no live master after them",
            evidence,
            "a failed master launch with no live binding after it",
            len(failed),
        )
    ]


def as_of(record, at):
    """The record as the Doctor could read it at a moment: later rows, lines and bindings not yet written."""
    retried = _retried(record["transfers"])

    def seen(row):
        binding = row["binding"]
        when = _failed_at(row, retried) if binding["state"] == "absent" else binding.get("at", row["at"])
        return row if when <= at else {**row, "binding": {"state": "pending"}}

    return {
        **record,
        "transfers": [seen(row) for row in record["transfers"] if row["at"] <= at],
        "restored": [o for o in record["restored"] if o["at"] <= at],
        "agents": [a for a in record["agents"] if a["started_at"] <= at],
        "journal": None if record["journal"] is None else [line for line in record["journal"] if line["at"] <= at],
    }


def journal_hour(record, at):
    """The failed-spawn detector as a Doctor pass ran it: master spawn failed lines from the last journal hour."""
    lines = [line["message"] for line in record["journal"] if at - JOURNAL_MS <= line["at"] <= at]
    return spawns.failed({"slug": record["slug"], "actions": lines})


def latest(record, at):
    return findings(record)


DETECTORS = (journal_hour, latest)


def _shown(finding, stored, at, cooldown_ms):
    verdict = (stored or {}).get("verdict")
    if not verdict or verdict["at"] > at:
        return True
    grew = finding.measure > verdict["measure"] and list(finding.evidence) != verdict["evidence"]
    return grew and at >= verdict["at"] + cooldown_ms


def _covered(record, at, verdicts, cooldown_ms, detectors):
    return any(
        _shown(f, verdicts.get(f.id), at, cooldown_ms)
        for detect in detectors
        for f in detect(record, at)
        if f.kind == KIND or f.id == OLD_ID
    )


def missed(record, verdicts, cooldown_ms, window, detectors):
    """Failed master launches inside the window that no detector would put before the Doctor master at the pass
    after the failure, judged against the verdicts given before that pass."""
    if record["journal"] is None:
        raise Unavailable(f"{MISSED} unavailable: {record['journal_error']}")
    since, until = window
    return sum(
        1
        for a in attempts(record)
        if since <= a.at <= until
        and not _covered(as_of(record, a.at + MATCH_MS), a.at + MATCH_MS, verdicts, cooldown_ms, detectors)
    )
