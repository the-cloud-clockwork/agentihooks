"""Every task second in the window lands in exactly one named stage.

Base phases run claim wait, build, ci run and review; overlays take an instant from its base in a fixed priority,
and an instant of claim wait while host samples hold spawns is that holder's hold. Claim wait starts when the task
became claimable and is reported as seat wait or queue wait.
"""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from scripts.swarm import bottleneck, freeze, metrics_outbox
from scripts.swarm.store import RedisStore
from scripts.swarm_ledger.repository import hierarchy

KEY = "bottleneck-stages"
TOP = 3
SOURCES = (*bottleneck.SOURCES, "ci_stages", "ledger_events")
READY = ("added", "task open", "back in scope")
PHASE_OPENS = ("review changed", "planning changed", "depends_on changed", "back in scope")
CHECKED, FOCUSED, UNFROZEN = "checked", "focused", "unfrozen"
CLAIM_WAIT, SEAT_WAIT, QUEUE_WAIT = "claim_wait", "seat_wait", "queue_wait"
LANE = "eng"
DONE = "task done"
JOINED = "joined"
OVERLAYS = ("merge_queue", "intent_fix", "launch_failure", "launch", "claim_wait", "ci_overrun", "ci_queue")
INTENT = ("intent_failed", "intent_passed")
NO_FIX_PHASE = ""
SPOKEN = {"ci_overrun": "in", "review": "in"}
TABLE = metrics_outbox.Table(
    "bottleneck_stages",
    (("stage", "String"), ("label", "String"), ("seconds", "Float64"), ("window_s", "Float64")),
)


@dataclass(frozen=True)
class Span:
    stage: str
    start: float
    end: float
    label: str = ""

    def covers(self, at: float) -> bool:
        return self.start <= at < self.end


@dataclass(frozen=True)
class Timeline:
    start: float
    end: float
    base: tuple
    overlays: tuple
    task: str = ""
    lane: str = LANE
    seats: tuple = ()


def _ready(task: dict, marks: list, before: float, ready: "Claim") -> float | None:
    opens = ready.opens(task)
    return max((mark.at for mark in marks if mark.at <= before and opens(mark)), default=None)


def _lives(task_id: str, rows: dict, end: float) -> list[tuple[tuple, Span]]:
    mine = [event for event in rows["agent_events"] if event["task"] == task_id]
    stops = {event["agent"]: event["ts_ms"] for event in mine if event["kind"] == "retire"}
    failed = {event["agent"] for event in mine if event["kind"] == "launch_failed"}
    waiting = {event["agent"] for event in mine if event["kind"] == "launch_waiting"}
    joins = [event for event in rows["ledger_events"] if event["kind"] == JOINED]
    found = []
    for spawn in mine:
        name = spawn["agent"]
        if spawn["kind"] != "spawn" or name in waiting:
            continue
        start, stop = spawn["ts_ms"], min(stops.get(name, end), end)
        if name in failed:
            found.append(((start, stop), Span("launch_failure", start, stop, spawn["harness"])))
            continue
        joined = min(
            (event["ts_ms"] for event in joins if event["by"] == name and event["ts_ms"] >= start), default=stop
        )
        found.append(((start, stop), Span("launch", start, min(joined, stop), spawn["harness"])))
    return found


def _gaps(lives: list[tuple], start: float, end: float) -> list[Span]:
    gaps, cursor = [], start
    for begin, stop in sorted(lives):
        gaps.append(Span("claim_wait", cursor, min(begin, end)))
        cursor = max(cursor, stop)
    return [*gaps, Span("claim_wait", cursor, end)]


def _ci(runs: list, stage_rows: list, pushed: float, green: float) -> list[Span]:
    spans = []
    for run in runs:
        created, ended = bottleneck._created(run), run["ts_ms"]
        mine = [row for row in stage_rows if (row["run_id"], row["attempt"]) == (run["run_id"], run["attempt"])]
        slowest = max(mine, key=lambda row: (row["pickup_s"], row["stage"]), default={})
        queued = (run["queue_s"] + slowest.get("pickup_s", 0.0)) * 1000
        spans.append(Span("ci_queue", created, min(ended, created + queued), slowest.get("runner", "")))
        cursor = ended
        for row in sorted(mine, key=lambda row: (-_over(row), row["stage"])):
            spans.append(Span("ci_overrun", max(created, cursor - _over(row) * 1000), cursor, row["stage"]))
            cursor -= _over(row) * 1000
    return [Span(span.stage, max(span.start, pushed), min(span.end, green), span.label) for span in spans]


def _over(row: dict) -> float:
    return max(0.0, row["seconds"] - row["budget"]) if row["budget"] else 0.0


def _reviews(task_id: str, rows: dict, end: float) -> list[Span]:
    mine = sorted(
        (event for event in rows["delivery_events"] if event["task"] == task_id), key=lambda event: event["ts_ms"]
    )
    verdicts = [event for event in mine if event["kind"] in INTENT]
    spans = [
        Span("intent_fix", event["ts_ms"], next((later["ts_ms"] for later in verdicts[index + 1 :]), end))
        for index, event in enumerate(verdicts)
        if event["kind"] == "intent_failed"
    ]
    queued = [event["ts_ms"] for event in mine if event["kind"] == "merge_queued"]
    return spans + ([Span("merge_queue", queued[0], end)] if queued else [])


def _rounds(task_id: str, rows: dict, green: float, end: float) -> list[Span]:
    marks = [
        event["ts_ms"]
        for event in rows["delivery_events"]
        if event["task"] == task_id and event["kind"] == "review_round"
    ]
    edges = sorted({green, end, *(min(max(mark, green), end) for mark in marks)})
    return [
        Span("review", start, stop, f"round {1 + sum(mark <= start for mark in marks)}")
        for start, stop in zip(edges, edges[1:])
    ]


def _claimed(task: dict, rows: dict, now_ms: int, live: bool, ready: "Claim", marks: list) -> Timeline:
    task_id = task["id"]
    mine = [event for event in rows["agent_events"] if event["task"] == task_id]
    claimed = min(event["ts_ms"] for event in mine if event["kind"] == "claim")
    merges = [
        event["ts_ms"] for event in rows["delivery_events"] if event["task"] == task_id and event["kind"] == "merge"
    ]
    end = merges[0] if merges else (now_ms if live else max(event["ts_ms"] for event in mine))
    branch = task.get("branch")
    runs = [run for run in rows["ci_runs"] if branch and run["branch"] == branch]
    opened = [
        event["ts_ms"]
        for event in rows["delivery_events"]
        if event["task"] == task_id and event["kind"] == "pull_request_opened"
    ]
    pushed = min([at for at in [*map(bottleneck._created, runs), *opened] if at >= claimed] + [end])
    green = bottleneck._green(runs, pushed, end)
    claimable_at = _ready(task, marks, claimed, ready)
    start = claimed if claimable_at is None else claimable_at
    lives = _lives(task_id, rows, end)
    base = (
        Span("claim_wait", start, claimed),
        Span("build", claimed, pushed),
        Span("ci_run", pushed, green),
        *_rounds(task_id, rows, green, end),
    )
    overlays = (
        *_reviews(task_id, rows, end),
        *(launch for _, launch in lives),
        *_gaps([life for life, _ in lives], claimed, pushed),
        *_ci(runs, rows["ci_stages"], pushed, green),
    )
    seats = tuple(life for life, _ in lives)
    return Timeline(start, end, base, overlays, task_id, task.get("lane", LANE), seats)


def _waiting(task: dict, marks: list, ready: "Claim", now_ms: int) -> Timeline | None:
    if not ready(task):
        return None
    start = _ready(task, marks, now_ms, ready)
    if start is None:
        return None
    return Timeline(start, now_ms, (Span(CLAIM_WAIT, start, now_ms),), (), task["id"], task.get("lane", LANE))


class Claim:
    """The tick's claim rules over one ledger doc, the events that open a task to them, and the lane seat caps."""

    def __init__(self, doc: dict, fix_phase: str, seats: Mapping[str, int] | None = None):
        self.doc, self.fix_phase, self.seats = doc, fix_phase, seats
        self.rows = {task["id"]: task for task in doc.get("tasks", [])}
        self.graph = hierarchy.project(doc)[0]

    def __call__(self, task: dict) -> bool:
        from scripts.swarm import tick

        return tick.ready(task, self.rows, self.doc, self.graph, self.fix_phase)

    def opens(self, task: dict) -> Callable[["Mark"], bool]:
        chain = freeze.ancestry(task, self.graph)
        phase = next((row for row in self.doc.get("phases", []) if row["id"] == task.get("phase")), {})
        node = f"phases/{phase['id']}" if phase else None
        before = {f"phases/{name}" for name in phase.get("depends_on") or []}
        needed = {f"tasks/{name}" for name in task.get("depends_on") or []}

        def covers(target: str) -> bool:
            return freeze.covers(target, task, chain)

        def opens(mark: Mark) -> bool:
            kind, target = mark.event["kind"], mark.event["target"]
            return (
                (target == chain[0] and kind in READY)
                or (kind == DONE and target in needed)
                or (kind == CHECKED and target in before)
                or (target == node and kind in PHASE_OPENS)
                or (kind == FOCUSED and covers(target))
                or (
                    (any(map(covers, mark.cleared)) or bool(mark.released))
                    and (not mark.focus or any(map(covers, mark.focus)))
                )
            )

        return opens


@dataclass(frozen=True)
class Mark:
    """A ledger event with the focus targets an unfreeze released and the focus still live after it."""

    event: dict
    cleared: tuple = ()
    released: frozenset = frozenset()
    focus: frozenset = frozenset()

    @property
    def at(self) -> float:
        return self.event["ts_ms"]


def history(events: list) -> list[Mark]:
    live, marks = set(), []
    for event in sorted(events, key=lambda event: event["ts_ms"]):
        cleared = tuple(_cleared(event)) if event["kind"] == UNFROZEN else ()
        released = frozenset(live.intersection(cleared))
        if event["kind"] == FOCUSED:
            live.add(event["target"])
        live.difference_update(cleared)
        marks.append(Mark(event, cleared, released, frozenset(live)))
    return marks


def _cleared(event: dict) -> list:
    return json.loads(event.get("payload") or "{}").get("cleared") or [event["target"]]


def claimable(doc: dict, fix_phase: str, seats: Mapping[str, int] | None = None) -> Claim:
    return Claim(doc, fix_phase, seats)


def timelines(rows: dict, tasks: list, now_ms: int, ready: Claim | None = None) -> list[Timeline]:
    claimed = {event["task"] for event in rows["agent_events"] if event["kind"] == "claim"}
    by_id = {task["id"]: task for task in tasks}
    ready = ready or claimable({"tasks": tasks}, NO_FIX_PHASE)
    marks = history(rows["ledger_events"])
    found = [
        _claimed(
            by_id.get(task_id, {"id": task_id}),
            rows,
            now_ms,
            by_id.get(task_id, {}).get("state") in bottleneck.OPEN,
            ready,
            marks,
        )
        for task_id in sorted(claimed)
    ]
    waiting = [_waiting(task, marks, ready, now_ms) for task in tasks if task["id"] not in claimed]
    return found + [timeline for timeline in waiting if timeline is not None]


def holds(samples: list, now_ms: int) -> list[Span]:
    held = [
        Span(f"{share}_hold", start, end, harness)
        for share, start, end, count, harness in bottleneck.held_spans(samples, now_ms)
        if count > 0
    ]
    merged = []
    for span in held:
        if (
            merged
            and merged[-1].stage == span.stage
            and merged[-1].label == span.label
            and merged[-1].end == span.start
        ):
            merged[-1] = Span(span.stage, merged[-1].start, span.end, span.label)
        else:
            merged.append(span)
    return merged


def _stage_at(timeline: Timeline, held: list[Span], at: float) -> tuple[str, str]:
    ranked = sorted(timeline.overlays, key=lambda span: OVERLAYS.index(span.stage))
    span = next(span for span in (*ranked, *timeline.base) if span.covers(at))
    if span.stage == CLAIM_WAIT:
        hold = next((hold for hold in held if hold.covers(at)), None)
        if hold is not None:
            return hold.stage, hold.label
    return span.stage, span.label


def _segments(timeline: Timeline, held: list[Span], window: bottleneck.Window):
    low = max(timeline.start, window.start)
    high = max(low, min(timeline.end, window.end))
    spans = [*timeline.base, *timeline.overlays, *held]
    points = sorted({low, high, *(min(max(edge, low), high) for span in spans for edge in (span.start, span.end))})
    for start, end in zip(points, points[1:]):
        yield (start, end, *_stage_at(timeline, held, (start + end) / 2))


def _charges(timeline: Timeline, held: list[Span], window: bottleneck.Window, span: Span) -> bool:
    return any((stage, label) == (span.stage, span.label) for _, _, stage, label in _segments(timeline, held, window))


def _split(pieces: list, lives: list, cap: int | None) -> list[tuple]:
    """Charge each lane's claim wait pieces as seat wait, at most one per seat while every seat is busy, else queue wait."""
    edges = {edge for _, start, end, _, _ in pieces for edge in (start, end)}
    points = sorted(edges | {edge for life in lives for edge in life})
    found = []
    for low, high in zip(points, points[1:]):
        waiting = sorted(piece for piece in pieces if piece[1] <= low and high <= piece[2])
        busy = sum(start <= low and high <= stop for start, stop in lives)
        if cap is None:
            seats = len(waiting)
        else:
            seats = cap if busy >= cap else 0
        for rank, (_, _, _, owner, label) in enumerate(waiting):
            found.append((owner, SEAT_WAIT if rank < seats else QUEUE_WAIT, label, (high - low) / 1000))
    return found


def _charged(rows: dict, tasks: list, now_ms: int, window_ms: int, ready: Claim | None) -> list[tuple]:
    window = bottleneck.Window(now_ms - window_ms, now_ms)
    held = holds(rows["host_samples"], now_ms)
    found = timelines(rows, tasks, now_ms, ready)
    seats = ready.seats if ready else None
    lives, pieces, seconds = {}, {}, [{} for _ in found]
    for index, timeline in enumerate(found):
        lives.setdefault(timeline.lane, []).extend(timeline.seats)
        for start, end, stage, label in _segments(timeline, held, window):
            if stage == CLAIM_WAIT:
                pieces.setdefault(timeline.lane, []).append(((timeline.start, timeline.task), start, end, index, label))
            else:
                seconds[index][stage, label] = seconds[index].get((stage, label), 0.0) + (end - start) / 1000
    for lane, waiting in pieces.items():
        cap = None if seats is None else seats.get(lane, 0)
        for index, stage, label, value in _split(waiting, lives[lane], cap):
            seconds[index][stage, label] = seconds[index].get((stage, label), 0.0) + value
    return list(zip(found, seconds))


def _overrun_episodes(rows, timeline, branch, held, window):
    episodes = set()
    for run in rows["ci_runs"]:
        if not branch or run["branch"] != branch:
            continue
        for span in _ci([run], rows["ci_stages"], timeline.start, timeline.end):
            if span.stage != "ci_overrun":
                continue
            clipped = bottleneck.Window(max(window.start, span.start), min(window.end, span.end))
            if _charges(timeline, held, clipped, span):
                episodes.add(str(run["run_id"]))
    return episodes


def observations(rows: dict, tasks: list, now_ms: int, window_ms: int, ready: Claim | None = None) -> dict:
    window = bottleneck.Window(now_ms - window_ms, now_ms)
    held = holds(rows["host_samples"], now_ms)
    branches = {task["id"]: task.get("branch") for task in tasks}
    found = {}
    for timeline, seconds in _charged(rows, tasks, now_ms, window_ms, ready):
        for stage, _ in seconds:
            if stage != "ci_overrun":
                found.setdefault(stage, set()).add(f"{stage}:{timeline.task}:{int(timeline.start)}")
        overruns = _overrun_episodes(rows, timeline, branches.get(timeline.task), held, window)
        if overruns:
            found.setdefault("ci_overrun", set()).update(overruns)
    return {stage: sorted(episodes) for stage, episodes in found.items()}


def report(
    rows: dict,
    tasks: list,
    now_ms: int,
    window_ms: int = bottleneck.WINDOW_MS,
    ready: Claim | None = None,
) -> dict:
    seconds = {}
    for _, charged in _charged(rows, tasks, now_ms, window_ms, ready):
        for key, value in charged.items():
            seconds[key] = seconds.get(key, 0.0) + value
    found = [{"stage": stage, "label": label, "seconds": value} for (stage, label), value in seconds.items()]
    found.sort(key=lambda row: (-row["seconds"], row["stage"], row["label"]))
    return {
        "at": now_ms,
        "window_hours": window_ms / 3_600_000,
        "stages": found,
        "total": sum(seconds.values()),
    }


def _named(row: dict) -> str:
    name = row["stage"].replace("_", " ")
    return f"{name} {SPOKEN.get(row['stage'], 'on')} {row['label']}" if row["label"] else name


def _reading(row):
    text = f"{_named(row)} {row['seconds'] / 3600:.1f} task hours"
    if "trend" in row:
        text += f" ({row['trend']})"
    return text


def line(found: dict, now_ms: int) -> str:
    if not found:
        return "top stages not measured yet: the metrics sink is off or no tick has run"
    hours = f"{found['window_hours']:g}"
    if not found["total"]:
        return f"top stages none: no task time in the last {hours} hours"
    top = ", ".join(_reading(row) for row in found["stages"][:TOP])
    age = (now_ms - found["at"]) // 60_000
    return (
        f"top stages in the last {hours} hours: {top}; {found['total'] / 3600:.1f} task hours in all,"
        f" measured {age} minutes ago"
    )


def read_rows(box: metrics_outbox.Outbox, slug: str, now_ms: int) -> dict:
    return {name: [found for found in box.recent(name, now_ms) if found["ledger"] == slug] for name in SOURCES}


def stage_rows(slug: str, found: dict) -> list[dict]:
    return [
        {
            "event_id": f"stage:{slug}:{found['at']}:{row['stage']}:{row['label']}",
            "ledger": slug,
            "ts_ms": found["at"],
            **dict.fromkeys(("plan", "phase", "slice", "task"), ""),
            "stage": row["stage"],
            "label": row["label"],
            "seconds": row["seconds"],
            "window_s": found["window_hours"] * 3600,
        }
        for row in found["stages"]
    ]


def record(box: metrics_outbox.Outbox, store: RedisStore, slug: str, now_ms: int, tasks: list) -> dict:
    found = report(read_rows(box, slug, now_ms), tasks, now_ms)
    box.append(TABLE, stage_rows(slug, found))
    store.redis.set(store.key(slug, KEY), json.dumps(found))
    return found


def read(store: RedisStore, slug: str) -> dict:
    value = store.redis.get(store.key(slug, KEY))
    return json.loads(value) if value else {}
