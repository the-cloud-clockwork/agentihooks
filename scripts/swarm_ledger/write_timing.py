import argparse
import json
import os
import resource
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.swarm_ledger import request_metrics

TEXT = "Timing one agent ledger write"


@dataclass(frozen=True)
class Child:
    code: int
    wall: float
    cpu: float


def spawned(argv: list[str], timing: Path) -> Child:
    """One write process run to its end with its own timing file: its exit code, wall and process CPU."""
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    started = time.perf_counter()
    environment = {**os.environ, request_metrics.TIMING_FILE: str(timing)}
    done = subprocess.run(argv, env=environment, stdout=subprocess.DEVNULL, check=False)
    wall = time.perf_counter() - started
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    return Child(done.returncode, wall, after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime)


def agent_comment(slug: str, name: str, task: str) -> list[str]:
    """The agentihooks api comment write an agent makes on its task, run through the console script's own entry
    module on this interpreter so the child runs the same code as the timer."""
    return [
        sys.executable,
        "-m",
        "scripts.install",
        "api",
        "--slug",
        slug,
        "--as",
        name,
        "comment",
        f"tasks/{task}",
        TEXT,
    ]


def timed_lines(timing: Path) -> list[dict[str, Any]]:
    """The timing lines one write process left, one per ledger request it made."""
    return [json.loads(line) for line in timing.read_text().splitlines()] if timing.exists() else []


def figures(child: Child, lines: list[dict[str, Any]]) -> dict[str, float]:
    """One write's figures: its process, its requests, its operations request and each substep, wall and CPU.

    Substeps nest: apply_locked holds the four write substeps and, on a cold copy, the copy substeps, which otherwise
    run inside guard_read."""
    writes = [line for line in lines if line["method"] == "POST"]
    row: dict[str, float] = {
        "process_wall": child.wall,
        "process_cpu": child.cpu,
        "requests": len(lines),
        "request_wall": sum(line["wall"] for line in lines),
        "request_cpu": sum(line["cpu"] for line in lines),
        "write_wall": sum(line["wall"] for line in writes),
        "write_cpu": sum(line["cpu"] for line in writes),
        "lock_wait": sum(line["lock_wait"] for line in lines),
        "postgres_wait": sum(line["postgres_wait"] for line in lines),
    }
    for line in lines:
        for name, spent in line["steps"].items():
            for measure in ("wall", "cpu"):
                key = f"{name}_{measure}"
                row[key] = row.get(key, 0.0) + spent[measure]
    return row


def spread(values: list[float]) -> dict[str, float]:
    """The median and max of one figure across the writes."""
    return {"median": statistics.median(values), "max": max(values)}


def summary(rows: list[dict[str, float]]) -> dict[str, dict[str, float]]:
    """The median and max of every figure, a write without a substep counting it as zero."""
    names = sorted({name for row in rows for name in row})
    return {name: spread([row.get(name, 0.0) for row in rows]) for name in names}


def parser() -> argparse.ArgumentParser:
    """The time writes command line."""
    found = argparse.ArgumentParser(description="Time agent comment writes on a ledger task, each a fresh process.")
    found.add_argument("--slug", required=True)
    found.add_argument("--as", dest="name", required=True)
    found.add_argument("--count", type=int, default=5)
    found.add_argument("task")
    return found


def main(argv: list[str] | None = None) -> None:
    """Run the writes one after another and print each write's figures with the median and max of each."""
    args = parser().parse_args(argv)
    count: int = args.count
    agent = agent_comment(args.slug, args.name, args.task)
    rows: list[dict[str, float]] = []
    before = os.getloadavg()
    with tempfile.TemporaryDirectory(prefix="ledger-write-timing-") as folder:
        for index in range(1, count + 1):
            timing = Path(folder) / f"write-{index}.jsonl"
            child = spawned(agent, timing)
            if child.code:
                print(f"write {index} of {count} exited {child.code}", file=sys.stderr)
                raise SystemExit(child.code)
            rows.append(figures(child, timed_lines(timing)))
    report = {
        "slug": args.slug,
        "task": args.task,
        "count": count,
        "load_before": before,
        "load_after": os.getloadavg(),
        "writes": rows,
        "summary": summary(rows),
    }
    print(json.dumps(report, indent=1))
