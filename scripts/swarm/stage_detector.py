from scripts.swarm import bottleneck, stages
from scripts.swarm.health import standing
from scripts.swarm.store import RedisStore


def _totals(found: dict) -> dict:
    totals = {}
    for row in found["stages"]:
        stage = row["stage"]
        total = totals.setdefault(stage, {"stage": stage, "seconds": 0.0, "labels": []})
        total["seconds"] += row["seconds"]
        if row["label"]:
            total["labels"].append(row["label"])
    return totals


def _episodes(rows: dict, stage: str, now_ms: int, window_ms: int) -> list[str]:
    if stage == "ci_overrun":
        return sorted(
            {
                str(row["run_id"])
                for row in rows["ci_stages"]
                if now_ms - window_ms < row["ts_ms"] <= now_ms and row["budget"] > 0 and row["seconds"] > row["budget"]
            }
        )
    return [stage]


def report(rows: dict, tasks: list, now_ms: int, window_ms: int = bottleneck.WINDOW_MS) -> dict:
    current = stages.report(rows, tasks, now_ms, window_ms)
    previous = _totals(stages.report(rows, tasks, now_ms - window_ms, window_ms))
    ranked = []
    for stage, row in _totals(current).items():
        before = previous.get(stage, {}).get("seconds", 0.0)
        change = row["seconds"] - before
        ranked.append(
            {
                **row,
                "label": ", ".join(sorted(row["labels"])),
                "previous_seconds": before,
                "trend": "rising" if change > 0 else "falling" if change < 0 else "unchanged",
                "episodes": _episodes(rows, stage, now_ms, window_ms),
            }
        )
    ranked.sort(key=lambda row: (-row["seconds"], row["stage"]))
    return {**current, "stages": ranked, "top": ranked[:3]}


def update_findings(store: RedisStore, slug: str, found: dict, now_ms: int) -> None:
    for row in found["stages"]:
        standing.update_detector(store.redis, slug, row, now_ms)
