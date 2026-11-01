import argparse
import json
import math
from collections import defaultdict
from statistics import median
from typing import Any

from scripts.swarm.timing import HISTORY_LIMIT

PAGE_SIZE = 256


def p95(values: list[float]) -> float | None:
    return sorted(values)[math.ceil(len(values) * 0.95) - 1] if values else None


def summarize(passes: list[dict[str, Any]], steps: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(passes, key=lambda row: row["start_ms"])
    identities = {row["pass_id"] for row in ordered}
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    present: set[str] = set()
    orphan = 0
    for row in steps:
        if row["pass_id"] not in identities:
            orphan += 1
            continue
        present.add(row["pass_id"])
        if row["step"] != "run_tick":
            grouped[row["step"]].append(row)
    costs = {name: sum(row["own_cpu_s"] + row["reaped_child_cpu_s"] for row in rows) for name, rows in grouped.items()}
    cpu = sum(costs.values())
    gaps = [max(0.0, (right["start_ms"] - left["end_ms"]) / 1000) for left, right in zip(ordered, ordered[1:])]
    return {
        "pass_count": len(ordered),
        "pass_p95_s": p95([row["wall_s"] for row in ordered]),
        "gap_p95_s": p95(gaps),
        "gap_max_s": max(gaps) if gaps else None,
        "passes_without_steps": len(identities - present),
        "orphan_steps": orphan,
        "steps": [
            {
                "step": name,
                "count": len(rows),
                "median_s": median(row["wall_s"] for row in rows),
                "p95_s": p95([row["wall_s"] for row in rows]),
                "cpu_share": 100 * costs[name] / cpu if cpu else 0.0,
            }
            for name, rows in sorted(grouped.items())
        ],
    }


def history(store: Any, slug: str, stream: str, limit: int) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    cursor = "+"
    for offset in range(0, limit, PAGE_SIZE):
        rows = store.redis.xrevrange(store.key(slug, stream), max=cursor, count=min(PAGE_SIZE, limit - offset))
        if not rows:
            break
        records.extend(json.loads(fields["record"]) for _, fields in rows)
        cursor = f"({rows[-1][0]}"
    return records


def read(store: Any, slug: str, passes: int) -> dict[str, Any]:
    selected = history(store, slug, "tick:passes", passes)
    identities = {row["pass_id"] for row in selected}
    steps = [row for row in history(store, slug, "tick:steps", HISTORY_LIMIT) if row.get("pass_id") in identities]
    return summarize(selected, steps)


def pass_count(value: str) -> int:
    count = int(value)
    if not 1 <= count <= 50000:
        raise argparse.ArgumentTypeError("passes must be between 1 and 50000")
    return count


def seconds(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}s"


def command(store: Any, args: argparse.Namespace) -> None:
    report = read(store, args.slug, args.passes)
    print(f"Tick report: {report['pass_count']} recorded passes")
    print(f"Pass p95: {seconds(report['pass_p95_s'])}")
    print(f"Gap p95: {seconds(report['gap_p95_s'])}; largest gap: {seconds(report['gap_max_s'])}")
    print(f"Passes without retained steps: {report['passes_without_steps']}")
    print("Step  Count  Median  P95  CPU share")
    for row in report["steps"]:
        print(
            f"{row['step']}  {row['count']}  {seconds(row['median_s'])}  {seconds(row['p95_s'])}  {row['cpu_share']:.2f}%"
        )
    print("CPU share uses summed own and reaped child CPU of retained steps, excluding the whole pass.")
