"""Shadow comparison of lab verdicts with GitHub Actions: one row per gate and head.

`Shadow.observe` reads a check_run.completed delivery. A github-actions check run gives the Actions verdict of the
gate its job name maps to, and a lab-shadow/<gate> check run gives the lab verdict and the grader revision its
summary names; the check runs of one side fold, failing when any failed. Once a gate and head hold both verdicts,
`Shadow.record` writes the row to Redis agentihooks-ci:shadow:<gate>:<head>, once per verdict pair under
lab/shadow/ and to the lab_shadow table, and counts ci_lab_verdict_mismatch_total{gate} when the row turns into
a mismatch. python -m scripts.ci_lab shadow report --since REVISION prints heads compared and mismatches per gate.
"""

import argparse
import functools
import hashlib
import json
import re
import subprocess
import sys
import time
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from scripts import ci_wiring
from scripts.ci_lab import contract, metrics, static_gates
from scripts.ci_lab.objects import ROOT, Objects
from scripts.ci_lab.store import PREFIX as STORE_PREFIX
from scripts.ci_lab.webhooks import CHECK_PREFIX
from scripts.swarm import metrics_outbox

__all__ = [
    "ACTIONS",
    "MUTATION",
    "OBJECTS",
    "PREFIX",
    "TABLE",
    "TESTS",
    "UNMAPPED",
    "UNMAPPED_KEY",
    "Row",
    "Shadow",
    "Sources",
    "ancestor",
    "gate_of",
    "grader_revision",
    "main",
    "report",
    "rows",
    "static_jobs",
]

ACTIONS = "github-actions"
LAB = "lab"
COMPLETED = "completed"
TESTS = "tests"
MUTATION = "mutation"
UNIT = "unit"
UNMAPPED = "unmapped"
PASS = "pass"
FAIL = "fail"
VERDICTS = {"success": PASS, "failure": FAIL, "timed_out": FAIL}
PREFIX = f"{STORE_PREFIX}shadow:"
UNMAPPED_KEY = f"{STORE_PREFIX}shadow-unmapped"
OBJECTS = f"{ROOT}shadow/"
LAB_NAME = f"{CHECK_PREFIX}-shadow/"
MATRIX = re.compile(r" \(.*\)$")
GRADER = re.compile(r"^Grader revision: (\S+)$", re.MULTILINE)
TABLE = metrics_outbox.Table(
    "lab_shadow",
    (
        ("head", "String"),
        ("gate", "String"),
        ("grader", "String"),
        ("lab_verdict", "String"),
        ("actions_verdict", "String"),
        ("mismatch", "Int64"),
    ),
)


@dataclass(frozen=True)
class Row:
    """One gate of one head with both verdicts, the grader revision the lab graded it with and whether they differ."""

    head: str
    gate: str
    grader: str
    lab_verdict: str
    actions_verdict: str
    mismatch: bool


@dataclass(frozen=True)
class Sources:
    """Where each side's verdicts come from: the static gate ids Actions jobs map to and the lab's GitHub App slug."""

    static: Collection[str]
    lab_app: str


def gate_of(name: str, static: Collection[str]) -> str:
    """Return the gate a check run name maps to: unit* to tests, mutation* to mutation, a static job or one of its
    steps to the job's gate id, anything else unmapped; a matrix suffix and the reusable workflow separator drop."""
    job = MATRIX.sub("", name).replace(" / ", "/")
    if job == TESTS or job.startswith(UNIT):
        return TESTS
    if job.startswith(MUTATION):
        return MUTATION
    return next((label for label in static if job == label or job.startswith(f"{label}/")), UNMAPPED)


def grader_revision(summary: str | None) -> str:
    """Return the grader revision a lab check run summary names, or an empty string when it names none."""
    match = GRADER.search(str(summary))
    return "" if match is None else match.group(1)


def static_jobs(root: Path) -> frozenset[str]:
    """Return the gate id of every static job of the workflow tree at root whose steps gate."""
    found = static_gates.discover((root / static_gates.WORKFLOW).read_text(), reusable=ci_wiring.load(root))
    return frozenset(item.job.partition("/")[0] for item in found)


def _fold(stored: Mapping[str, str], side: str) -> str | None:
    found = {value for field, value in stored.items() if field.startswith(f"{side}:")}
    if not found:
        return None
    return FAIL if FAIL in found else PASS


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _table_row(row: Row, ts_ms: int) -> dict[str, object]:
    parts = (row.gate, row.head, row.lab_verdict, row.actions_verdict)
    event = hashlib.sha256("\0".join(parts).encode()).hexdigest()
    base: dict[str, object] = dict.fromkeys(metrics_outbox.BASE_NAMES, "")
    fields = {"event_id": event, "ledger": metrics.LEDGER, "ts_ms": ts_ms, "mismatch": int(row.mismatch)}
    return {**base, **asdict(row), **fields}


class Shadow:
    """The shadow recorder: folds check_run.completed deliveries into one row per gate and head.

    Its caller observes deliveries one at a time, as one scheduler will once it feeds the recorder, so a turn into a
    mismatch is read and written by one observe at a time and counted once by the process registry."""

    def __init__(
        self,
        client: Any,
        objects: Objects,
        sink: metrics.Sink,
        registry: metrics.Registry,
        sources: Sources,
        now_ms: Callable[[], int] = _now_ms,
    ) -> None:
        self.client = client
        self.objects = objects
        self.sink = sink
        self.registry = registry
        self.sources = sources
        self.now_ms = now_ms

    def observe(self, payload: Mapping[str, Any]) -> Row | None:
        """Fold one check run delivery into its gate and head; return the row once both verdicts are known.

        A lab-shadow check run counts only from the lab's App and every other check run only from github-actions."""
        run = payload["check_run"]
        name = str(run["name"])
        lab = name.startswith(LAB_NAME)
        if payload["action"] != COMPLETED or run["app"]["slug"] != (self.sources.lab_app if lab else ACTIONS):
            return None
        gate = gate_of(name.removeprefix(LAB_NAME), self.sources.static)
        if gate == UNMAPPED:
            self.client.sadd(UNMAPPED_KEY, name)
            return None
        outcome = VERDICTS.get(str(run["conclusion"]))
        if outcome is None:
            return None
        head = str(run["head_sha"])
        key = f"{PREFIX}{gate}:{head}"
        fields = {f"{LAB if lab else ACTIONS}:{name}": outcome}
        if lab:
            fields["grader"] = grader_revision(run["output"]["summary"])
        self.client.hset(key, mapping=fields)
        stored = self.client.hgetall(key)
        lab_verdict, actions_verdict = _fold(stored, LAB), _fold(stored, ACTIONS)
        if lab_verdict is None or actions_verdict is None:
            return None
        return self.record(head, gate, lab_verdict, actions_verdict, stored["grader"])

    def record(self, head: str, gate: str, lab_verdict: str, actions_verdict: str, grader: str) -> Row:
        """Write one gate and head's row to Redis, the object store and lab_shadow; count a turn into a mismatch."""
        row = Row(head, gate, grader, lab_verdict, actions_verdict, lab_verdict != actions_verdict)
        key = f"{PREFIX}{gate}:{head}"
        before = self.client.hget(key, "mismatch")
        self.client.hset(key, mapping={**asdict(row), "mismatch": int(row.mismatch)})
        document = json.dumps(asdict(row), sort_keys=True).encode()
        self.objects.write_once(f"{OBJECTS}{gate}/{head}/{lab_verdict}-{actions_verdict}.json", document)
        self.sink.append(TABLE, [_table_row(row, self.now_ms())])
        if row.mismatch and before != "1":
            self.registry.inc(contract.VERDICT_MISMATCH_TOTAL, 1.0, {"gate": gate})
        return row


def rows(client: Any) -> list[Row]:
    """Return every shadow row in Redis that holds both verdicts."""
    found: list[Row] = []
    for key in client.scan_iter(match=f"{PREFIX}*"):
        stored = client.hgetall(key)
        if "mismatch" in stored:
            verdicts = (stored["lab_verdict"], stored["actions_verdict"])
            found.append(Row(stored["head"], stored["gate"], stored["grader"], *verdicts, stored["mismatch"] == "1"))
    return found


def report(found: Iterable[Row], unmapped: Iterable[str], since: str, descends: Callable[[str, str], bool]) -> str:
    """Return heads compared, then per gate the rows compared and mismatches, over the rows whose grader revision is
    since or descends from it, then a line per unmapped check run name."""
    chosen = [row for row in found if descends(since, row.grader)]
    lines = [f"since {since}", f"heads compared {len({row.head for row in chosen})}"]
    for gate in sorted({row.gate for row in chosen}):
        gated = [row for row in chosen if row.gate == gate]
        lines.append(f"gate {gate} compared {len(gated)} mismatches {sum(row.mismatch for row in gated)}")
    lines.extend(f"unmapped {name}" for name in sorted(unmapped))
    return "\n".join(lines) + "\n"


def ancestor(repo: Path, older: str, newer: str) -> bool:
    """Whether older is newer or one of its ancestors in the git repository at repo."""
    command = ["git", "-C", str(repo), "merge-base", "--is-ancestor", older, newer]
    return subprocess.run(command, capture_output=True).returncode == 0


def main(argv: Sequence[str], environ: Mapping[str, str]) -> int:
    """Print the shadow report: python -m scripts.ci_lab shadow report --since REVISION [--repo PATH] [--redis URL]."""
    parser = argparse.ArgumentParser(prog="python -m scripts.ci_lab shadow")
    report_parser = parser.add_subparsers(required=True).add_parser("report")
    report_parser.add_argument("--since", required=True)
    report_parser.add_argument("--repo", default=".")
    report_parser.add_argument("--redis", default=environ.get(contract.REDIS_URL))
    arguments = parser.parse_args(argv)
    if not arguments.redis:
        report_parser.error(f"--redis or {contract.REDIS_URL} names the lab Redis")
    descends = functools.cache(functools.partial(ancestor, Path(arguments.repo)))
    if not descends(arguments.since, arguments.since):
        report_parser.error(f"--since {arguments.since} names no commit in {arguments.repo}")
    import redis

    factory: Any = redis.Redis
    client = factory.from_url(arguments.redis, decode_responses=True)
    sys.stdout.write(report(rows(client), client.smembers(UNMAPPED_KEY), arguments.since, descends))
    return 0
