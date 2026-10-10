import json
from dataclasses import dataclass

from scripts.swarm import metrics_outbox
from scripts.swarm.store import RedisStore

WINDOW_MS = 4 * 60 * 60 * 1000
SAMPLE_GAP_MS = 5 * 60 * 1000
KEY = "bottleneck"
OPEN = ("claimed", "pr")
SOURCES = ("agent_events", "delivery_events", "ci_runs", "host_samples")
SHARES = ("engineering", "ci", "review", "host", "quota")
LABELS = {
    "engineering": "engineering",
    "ci": "CI, push to green checks",
    "review": "review and queue",
    "host": "host held spawns",
    "quota": "quota held spawns",
}
TABLE = metrics_outbox.Table(
    "bottlenecks",
    (("bottleneck", "String"), *((f"{share}_s", "Float64") for share in SHARES), ("total_s", "Float64")),
)


@dataclass(frozen=True)
class Window:
    start: int
    end: int

    def seconds(self, start: float, end: float) -> float:
        return max(0, min(end, self.end) - max(start, self.start)) / 1000


def _created(run: dict) -> float:
    return run["ts_ms"] - (run["queue_s"] + run["run_s"]) * 1000


def _green(runs: list, pushed: float, end: float) -> float:
    ended = sorted((run for run in runs if pushed <= run["ts_ms"] <= end), key=lambda run: run["ts_ms"])
    red = max((index for index, run in enumerate(ended) if run["conclusion"] != "success"), default=-1)
    later = ended[red + 1 :]
    return later[0]["ts_ms"] if later else end


def task_spans(rows: dict, tasks: list, now_ms: int) -> list[tuple]:
    claims, seen, opened = {}, {}, {}
    for event in rows["agent_events"]:
        seen[event["task"]] = max(seen.get(event["task"], event["ts_ms"]), event["ts_ms"])
        if event["kind"] == "claim":
            claims[event["task"]] = min(claims.get(event["task"], event["ts_ms"]), event["ts_ms"])
    merges = {event["task"]: event["ts_ms"] for event in rows["delivery_events"] if event["kind"] == "merge"}
    for event in rows["delivery_events"]:
        if event["kind"] == "pull_request_opened":
            opened.setdefault(event["task"], []).append(event["ts_ms"])
    branches = {task["id"]: task.get("branch") for task in tasks}
    live = {task["id"] for task in tasks if task.get("state") in OPEN}
    spans = []
    for task, claimed in claims.items():
        end = merges.get(task) or (now_ms if task in live else seen[task])
        runs = [run for run in rows["ci_runs"] if branches.get(task) and run["branch"] == branches[task]]
        pushes = [at for at in [*map(_created, runs), *opened.get(task, [])] if at >= claimed]
        pushed = min([*pushes, end])
        green = _green(runs, pushed, end)
        spans += [("engineering", claimed, pushed), ("ci", pushed, green), ("review", green, end)]
    return spans


def held_spans(samples: list, now_ms: int) -> list[tuple]:
    ordered = sorted(samples, key=lambda sample: sample["ts_ms"])
    nexts = [sample["ts_ms"] for sample in ordered[1:]] + [now_ms]
    return [
        (
            "host" if sample.get("held_by") == "host" else "quota",
            sample["ts_ms"],
            min(sample["ts_ms"] + SAMPLE_GAP_MS, after),
            sample["held_spawns"],
        )
        for sample, after in zip(ordered, nexts)
    ]


def report(rows: dict, tasks: list, now_ms: int, window_ms: int = WINDOW_MS) -> dict:
    window = Window(now_ms - window_ms, now_ms)
    seconds = dict.fromkeys(SHARES, 0.0)
    for share, start, end in task_spans(rows, tasks, now_ms):
        seconds[share] += window.seconds(start, end)
    for share, start, end, held in held_spans(rows["host_samples"], now_ms):
        seconds[share] += held * window.seconds(start, end)
    total = sum(seconds.values())
    return {
        "at": now_ms,
        "window_hours": window_ms / 3_600_000,
        "seconds": {share: round(value, 1) for share, value in seconds.items()},
        "total": round(total, 1),
        "bottleneck": max(SHARES, key=seconds.get) if total else "",
    }


def line(found: dict, now_ms: int) -> str:
    if not found:
        return "bottleneck not measured yet: the metrics sink is off or no tick has run"
    hours = f"{found['window_hours']:g}"
    if not found["bottleneck"]:
        return f"bottleneck none: no delivery time or held spawns in the last {hours} hours"
    name, total = found["bottleneck"], found["total"]

    def percent(share):
        return f"{round(100 * found['seconds'][share] / total)} percent"

    others = ", ".join(f"{LABELS[share]} {percent(share)}" for share in SHARES if share != name)
    age = (now_ms - found["at"]) // 60_000
    return (
        f"bottleneck {LABELS[name]}: {percent(name)} of {total / 3600:.1f} task hours in the last {hours} hours,"
        f" measured {age} minutes ago; {others}"
    )


def read_rows(box: metrics_outbox.Outbox, slug: str, now_ms: int) -> dict:
    return {name: [found for found in box.recent(name, now_ms) if found["ledger"] == slug] for name in SOURCES}


def bottleneck_row(slug: str, found: dict) -> dict:
    return {
        "event_id": f"bottleneck:{slug}:{found['at']}",
        "ledger": slug,
        "ts_ms": found["at"],
        **dict.fromkeys(("plan", "phase", "slice", "task"), ""),
        "bottleneck": found["bottleneck"],
        **{f"{share}_s": found["seconds"][share] for share in SHARES},
        "total_s": found["total"],
    }


def record(box: metrics_outbox.Outbox, store: RedisStore, slug: str, now_ms: int, tasks: list) -> dict:
    found = report(read_rows(box, slug, now_ms), tasks, now_ms)
    box.append(TABLE, [bottleneck_row(slug, found)])
    store.redis.set(store.key(slug, KEY), json.dumps(found))
    return found


def read(store: RedisStore, slug: str) -> dict:
    value = store.redis.get(store.key(slug, KEY))
    return json.loads(value) if value else {}
