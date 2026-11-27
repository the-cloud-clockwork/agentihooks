"""The acceptance rule report: GitHub Actions jobs against lab-shadow check runs over the last heads of one gate.

python -m scripts.ci_lab shadow report --gate GATE [--heads N] [--event merge_group] [--app SLUG] reads the newest
completed Tests runs of the event and, for each distinct head, joins the github-actions jobs the shadow recorder maps to
the gate with the head's lab-shadow/<gate> check run from the lab App, named by --app or AGENTIHOOKS_LAB_APP. A head
whose jobs of the gate were cancelled, still run or never graded leaves the window; a unit job the workflow reran after
a lost runner counts by its rerun. It prints one row per head and the counts, and exits 0 only when the window holds N
heads with no mismatch, a lab verdict on 48 and a lab verdict completed first on 45 of every 50, rounded up, and 1
otherwise. For the pull request event it also asks for a lab verdict on 48 of every 50 inside the wait of the head's
lab-verdict job, counted from the start of that job, and a row that holds both ends with the seconds between them.
"""

import argparse
import json
import math
import subprocess
import sys
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

import scripts.ci_budget.rate_limit
from scripts.ci_lab import shadow
from scripts.ci_lab.webhooks import CHECK_PREFIX
from scripts.ci_lab_verdict import WAIT_S

__all__ = [
    "Head",
    "Rule",
    "Tally",
    "Verdict",
    "actions",
    "check_runs",
    "claims",
    "collect",
    "first",
    "head",
    "heads_of",
    "holds",
    "inside",
    "lab",
    "main",
    "mismatch",
    "pages",
    "render",
    "rerun",
    "rule",
    "tally",
]

gh_api = cast("Callable[..., subprocess.CompletedProcess[str]]", scripts.ci_budget.rate_limit.gh_api)

ROOT = Path(__file__).resolve().parents[2]
REPOSITORY = "the-cloud-clockwork/agentihooks"
LAB_APP = "AGENTIHOOKS_LAB_APP"
LISTED_NAME = f"{CHECK_PREFIX}/"
WORKFLOW = "test.yml"
PULL_REQUEST = "pull_request"
MERGE_GROUP = "merge_group"
CANCELLED = "cancelled"
MISSING = "missing"
JOB = "lab-verdict"
COLUMNS = "{:<12}  {:<7}  {:<7}  {:<5}  {}"
SECONDS = "{:<48}  {}"
PER_PAGE = 100
PAGES = 10
TIMEOUT_S = 30.0
HEADS = 50
PRESENT = 48
FIRST = 45
INSIDE = 48

Fetch = Callable[[str], Mapping[str, Any]]


@dataclass(frozen=True)
class Verdict:
    """One side's verdict on a gate of a head, pass or fail, and when its last graded check run completed."""

    outcome: str
    completed_at: datetime


@dataclass(frozen=True)
class Head:
    """One head's Actions verdict on a gate, its lab verdict and when its lab-verdict job started.

    The lab verdict is None when the lab posted no completed verdict, the start None when the head holds no started
    lab-verdict job.
    """

    sha: str
    actions: Verdict
    lab: Verdict | None
    started: datetime | None = None


@dataclass(frozen=True)
class Rule:
    """The acceptance rule over a window of heads: how many heads, lab verdicts present, lab verdicts first and, for
    an event whose job waits for the lab, lab verdicts inside that wait; None where no job waits."""

    heads: int
    present: int
    first: int
    inside: int | None = None


@dataclass(frozen=True)
class Tally:
    """The counts the rule judges: heads, lab verdicts present, mismatches, lab verdicts first and lab verdicts inside
    the wait of the lab-verdict job."""

    heads: int
    present: int
    mismatches: int
    first: int
    inside: int = 0


def rule(heads: int, event: str) -> Rule:
    """Return the rule for a window of heads: lab verdicts present on 48 and first on 45 of every 50, rounded up, and
    for the pull request event, whose lab-verdict job waits for the lab, also inside that wait on 48."""
    inside = math.ceil(heads * INSIDE / HEADS) if event == PULL_REQUEST else None
    return Rule(heads, math.ceil(heads * PRESENT / HEADS), math.ceil(heads * FIRST / HEADS), inside)


def completed(run: Mapping[str, Any]) -> datetime:
    """Return when a check run completed."""
    return datetime.fromisoformat(str(run["completed_at"]))


def rerun(name: str) -> str | None:
    """Return the name the workflow reruns a unit job under after a lost runner, or None for any other job."""
    job, matrix, rest = name.partition(" (")
    return f"{job} rerun ({rest}" if job == shadow.UNIT and matrix else None


def actions(runs: Sequence[Mapping[str, Any]], gate: str, static: Collection[str]) -> Verdict | None:
    """Return the Actions verdict of the gate's jobs on a head: fail when any graded job failed, else pass.

    None while one of them runs, when one was cancelled or when none graded, so the head leaves the window; a unit job
    whose lost runner rerun is on the head counts by that rerun.
    """
    jobs = {
        str(run["name"]): run
        for run in runs
        if run["app"]["slug"] == shadow.ACTIONS and shadow.gate_of(str(run["name"]), static) == gate
    }
    kept = [run for name, run in jobs.items() if rerun(name) not in jobs]
    if any(run["status"] != shadow.COMPLETED or run["conclusion"] == CANCELLED for run in kept):
        return None
    graded = [run for run in kept if run["conclusion"] in shadow.VERDICTS]
    if not graded:
        return None
    outcomes = {shadow.VERDICTS[run["conclusion"]] for run in graded}
    return Verdict(shadow.FAIL if shadow.FAIL in outcomes else shadow.PASS, max(map(completed, graded)))


def lab(runs: Sequence[Mapping[str, Any]], gate: str, app: str, listed: bool = False) -> Verdict | None:
    """Return the lab App's newest verdict on the gate, or None when it posted no completed pass or fail: its
    lab-shadow/<gate> check run and, for a listed gate, also the lab/<gate> check run it posts under authority."""
    prefixes = (shadow.LAB_NAME, LISTED_NAME) if listed else (shadow.LAB_NAME,)
    names = {f"{prefix}{gate}" for prefix in prefixes}
    posted = [run for run in runs if run["name"] in names and run["app"]["slug"] == app]
    if not posted:
        return None
    newest = max(posted, key=lambda run: int(run["id"]))
    outcome = shadow.VERDICTS.get(str(newest["conclusion"]))
    return None if outcome is None else Verdict(outcome, completed(newest))


def started(runs: Sequence[Mapping[str, Any]]) -> datetime | None:
    """Return when the newest lab-verdict job of GitHub Actions on a head started, or None when none has started."""
    jobs = [run for run in runs if run["name"] == JOB and run["app"]["slug"] == shadow.ACTIONS]
    if not jobs:
        return None
    start = max(jobs, key=lambda run: int(run["id"])).get("started_at")
    return None if start is None else datetime.fromisoformat(str(start))


def head(
    sha: str, runs: Sequence[Mapping[str, Any]], gate: str, sources: shadow.Sources, listed: bool = False
) -> Head | None:
    """Return the head's verdicts on the gate and the start of its lab-verdict job, or None when it holds no Actions
    verdict."""
    found = actions(runs, gate, sources.static)
    if found is None:
        return None
    return Head(sha, found, lab(runs, gate, sources.lab_app, listed), started(runs))


def mismatch(found: Head) -> bool:
    """Whether the lab verdict disagrees with Actions on the head's success."""
    return found.lab is not None and found.lab.outcome != found.actions.outcome


def first(found: Head) -> bool:
    """Whether the lab verdict completed before the last graded Actions job of the gate."""
    return found.lab is not None and found.lab.completed_at < found.actions.completed_at


def waited(found: Head) -> timedelta | None:
    """Return the time from the start of the head's lab-verdict job to its lab verdict, or None without either."""
    if found.lab is None or found.started is None:
        return None
    return found.lab.completed_at - found.started


def inside(found: Head) -> bool:
    """Whether the lab verdict completed inside the wait of the head's lab-verdict job, counted from the job's start,
    which is earlier than the start of the wait by the job's setup steps."""
    taken = waited(found)
    return taken is not None and taken <= timedelta(seconds=WAIT_S)


def tally(heads: Sequence[Head]) -> Tally:
    """Return the counts the rule judges."""
    present = sum(found.lab is not None for found in heads)
    return Tally(len(heads), present, sum(map(mismatch, heads)), sum(map(first, heads)), sum(map(inside, heads)))


def holds(counted: Tally, window: Rule) -> bool:
    """Whether the counts meet the rule: the window full, no mismatch, enough lab verdicts present and first and, where
    the rule asks for it, enough inside the wait."""
    full = counted.heads >= window.heads and counted.mismatches == 0
    timely = window.inside is None or counted.inside >= window.inside
    return full and timely and counted.present >= window.present and counted.first >= window.first


def line(found: Head) -> str:
    """Return one head's row of the table."""
    if found.lab is None:
        return COLUMNS.format(found.sha[:12], found.actions.outcome, MISSING, "", "").rstrip()
    order = "first" if first(found) else "late"
    agreement = "mismatch" if mismatch(found) else "match"
    return COLUMNS.format(found.sha[:12], found.actions.outcome, found.lab.outcome, order, agreement)


def timed(found: Head) -> str:
    """Return one head's row ending with the seconds from the start of its lab-verdict job to its lab verdict, rounded
    up so a row reads the wait or less only when its verdict is inside it, or its plain row when the head lacks
    either."""
    taken = waited(found)
    return line(found) if taken is None else SECONDS.format(line(found), math.ceil(taken / timedelta(seconds=1)))


def render(gate: str, event: str, heads: Sequence[Head], window: Rule) -> str:
    """Return the report: the gate and event, one row per head, the counts against the rule and its verdict; a rule
    that asks for lab verdicts inside the wait adds the seconds column and that count."""
    counted = tally(heads)
    names = COLUMNS.format("head", "actions", "lab", "order", "agreement")
    waits = window.inside is not None
    lines = [
        f"gate {gate} event {event}",
        SECONDS.format(names, "seconds") if waits else names,
        *map(timed if waits else line, heads),
        f"heads {counted.heads} of {window.heads}",
        f"lab verdict present {counted.present} of at least {window.present}",
        f"mismatches {counted.mismatches} of at most 0",
        f"lab verdict first {counted.first} of at least {window.first}",
        *([f"lab verdict inside the wait {counted.inside} of at least {window.inside}"] if waits else []),
        "rule holds" if holds(counted, window) else "rule fails",
    ]
    return "".join(f"{text}\n" for text in lines)


def pages(fetch: Fetch, path: str, field: str) -> Iterator[Mapping[str, Any]]:
    """Yield every item of a paged GitHub list, stopping at the first short page or after PAGES pages."""
    for page in range(1, PAGES + 1):
        items = fetch(f"{path}&per_page={PER_PAGE}&page={page}")[field]
        yield from items
        if len(items) < PER_PAGE:
            return


def heads_of(fetch: Fetch, repository: str, event: str) -> Iterator[str]:
    """Yield the distinct head of each completed Tests run of the event, newest first."""
    seen: set[str] = set()
    runs = f"repos/{repository}/actions/workflows/{WORKFLOW}/runs?event={event}&status=completed"
    for run in pages(fetch, runs, "workflow_runs"):
        sha = str(run["head_sha"])
        if sha not in seen:
            seen.add(sha)
            yield sha


def check_runs(fetch: Fetch, repository: str, sha: str) -> list[Mapping[str, Any]]:
    """Return the latest check runs of a head, every page of them."""
    return list(pages(fetch, f"repos/{repository}/commits/{sha}/check-runs?filter=latest", "check_runs"))


def collect(fetch: Fetch, repository: str, event: str, gate: str, sources: shadow.Sources, count: int) -> list[Head]:
    """Return the newest count heads of the event that hold an Actions verdict on the gate."""
    found: list[Head] = []
    for sha in heads_of(fetch, repository, event):
        graded = head(sha, check_runs(fetch, repository, sha), gate, sources)
        if graded is not None:
            found.append(graded)
        if len(found) == count:
            break
    return found


def api(path: str) -> Mapping[str, Any]:
    """Return one GitHub API answer through the rate limited helper."""
    return json.loads(gh_api([path], timeout=TIMEOUT_S, text=True).stdout)


def claims(argv: Sequence[str]) -> bool:
    """Whether a shadow command is this report: shadow report with a gate, never the recorder's --since report."""
    return bool(argv) and argv[0] == "report" and any(arg == "--gate" or arg.startswith("--gate=") for arg in argv)


def main(argv: Sequence[str], environ: Mapping[str, str], fetch: Fetch = api) -> int:
    """Print the acceptance rule report of one gate and return 0 when the rule holds, 1 when it fails."""
    parser = argparse.ArgumentParser(prog="python -m scripts.ci_lab shadow report")
    parser.add_argument("--gate", required=True)
    parser.add_argument("--heads", type=int, default=HEADS)
    parser.add_argument("--event", choices=(PULL_REQUEST, MERGE_GROUP), default=PULL_REQUEST)
    parser.add_argument("--app", default=environ.get(LAB_APP))
    arguments = parser.parse_args(argv)
    if arguments.heads < 1:
        parser.error("--heads must be at least 1")
    if not arguments.app:
        parser.error(f"--app or {LAB_APP} names the lab App")
    repository = environ.get("GITHUB_REPOSITORY") or REPOSITORY
    window = rule(arguments.heads, arguments.event)
    sources = shadow.Sources(shadow.static_jobs(ROOT), arguments.app)
    found = collect(fetch, repository, arguments.event, arguments.gate, sources, arguments.heads)
    sys.stdout.write(render(arguments.gate, arguments.event, found, window))
    return 0 if holds(tally(found), window) else 1
