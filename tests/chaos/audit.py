import math
from collections import defaultdict

ENDS = frozenset({"exited", "exit-unrecorded"})
SETTLE_GRACE_MS = 60_000


def killed(records: list[dict]) -> dict[str, int]:
    return {r["target"]: r["t"] for r in records if r["kind"] == "fault" and r["fault"] == "hive-kill"}


def executions(records: list[dict]) -> list[dict]:
    deaths = killed(records)
    found, open_runs = [], defaultdict(list)
    for record in records:
        if record["kind"] == "launching":
            run = {"task": record["task"], "hive": record["proc"], "start": record["t"], "end": math.inf, "how": ""}
            run["end"] = deaths.get(run["hive"], math.inf)
            run["how"] = "killed" if run["end"] < math.inf else "running"
            open_runs[(record["proc"], record["task"])].append(run)
            found.append(run)
        elif record["kind"] in ENDS and open_runs[(record["proc"], record["task"])]:
            run = open_runs[(record["proc"], record["task"])].pop(0)
            run["end"], run["how"] = record["t"], record["kind"]
    return found


def duplicates(runs: list[dict], after: float = -math.inf, at: float = math.inf) -> list[dict]:
    by_task = defaultdict(list)
    for run in runs:
        if run["start"] <= at:
            by_task[run["task"]].append(run)
    pairs = []
    for task, held in by_task.items():
        held.sort(key=lambda run: run["start"])
        for index, first in enumerate(held):
            for second in held[index + 1 :]:
                if after <= second["start"] < first["end"]:
                    pairs.append({"task": task, "hives": [first["hive"], second["hive"]], "at": second["start"]})
    return pairs


def state(task: str, runs: list[dict], at: int) -> str:
    held = [run for run in runs if run["task"] == task and run["start"] <= at]
    if any(run["how"] == "exited" and run["end"] <= at for run in held):
        return "completed"
    if any(run["end"] > at for run in held):
        return "running"
    if any(run["how"] == "exit-unrecorded" for run in held):
        return "exit unrecorded"
    if any(run["how"] == "killed" for run in held):
        return "lost with its hive"
    return "never launched"


def orphans(records: list[dict], runs: list[dict], before: int, at: int) -> dict[str, str]:
    offered = {r["task"] for r in records if r["kind"] == "offered" and r["t"] <= before}
    found = {task: state(task, runs, at) for task in sorted(offered)}
    return {task: why for task, why in found.items() if why not in ("completed", "running")}


def settles(records: list[dict], after: float, at: int) -> dict:
    handled = defaultdict(list)
    for record in records:
        if record["kind"] == "settled" and record["t"] <= at:
            handled[record["claim"]].append(record["t"])
    exits = {r["claim"] for r in records if r["kind"] in ENDS and after <= r["t"] <= at - SETTLE_GRACE_MS}
    return {
        "handled_twice": sorted(claim for claim, times in handled.items() if len(times) > 1 and times[1] >= after),
        "never_handled": sorted(claim for claim in exits if claim not in handled),
    }


def _seconds(start: int, end: int | None) -> float | None:
    return None if end is None else round((end - start) / 1000, 3)


def _first(records: list[dict], kind: str, after: int, until: int, **match) -> int | None:
    for record in records:
        if after <= record["t"] <= until and record["kind"] == kind:
            if all(record.get(name) == value for name, value in match.items()):
                return record["t"]
    return None


def _held_by(records: list[dict], hive: str, at: int) -> list[str]:
    began = {r["task"] for r in records if r["kind"] == "launching" and r["proc"] == hive and r["t"] <= at}
    ended = {r["task"] for r in records if r["kind"] in ENDS and r["proc"] == hive and r["t"] <= at}
    return sorted(began - ended)


def hive_reclaim(records: list[dict], fault: dict, until: int) -> dict:
    tasks = {}
    for task in _held_by(records, fault["target"], fault["t"]):
        relaunch = next(
            (
                r["t"]
                for r in records
                if r["kind"] == "launched" and r["task"] == task and r["proc"] != fault["target"]
                if fault["t"] <= r["t"] <= until
            ),
            None,
        )
        tasks[task] = _seconds(fault["t"], relaunch)
    known = [value for value in tasks.values() if value is not None]
    return {"tasks": tasks, "max_s": max(known) if known else None}


def nats_reclaim(records: list[dict], fault: dict, until: int) -> dict:
    back = _first(records, "fault-cleared", fault["t"], until, fault="nats-restart")
    if back is None:
        return {"outage_s": None, "first_launch_s": None, "first_settle_s": None}
    return {
        "outage_s": _seconds(fault["t"], back),
        "first_launch_s": _seconds(back, _first(records, "launched", back, until)),
        "first_settle_s": _seconds(back, _first(records, "settled", back, until)),
    }


def hub_reclaim(records: list[dict], fault: dict, until: int) -> dict:
    takeover = next(
        (
            r["t"]
            for r in records
            if r["kind"] == "lease" and fault["t"] <= r["t"] <= until and r["holder"] not in (None, fault["target"])
        ),
        None,
    )
    settle = next(
        (
            r["t"]
            for r in records
            if r["kind"] == "settled" and fault["t"] <= r["t"] <= until and r["proc"] != fault["target"]
        ),
        None,
    )
    return {"takeover_s": _seconds(fault["t"], takeover), "first_settle_s": _seconds(fault["t"], settle)}


# Exits handled twice are reported but never fail the contract: JetStream redelivers at least once.
RECLAIM = {"hive-kill": hive_reclaim, "nats-restart": nats_reclaim, "hub-kill": hub_reclaim}
MEASURED = {
    "hive-kill": ("max_s",),
    "nats-restart": ("first_launch_s", "first_settle_s"),
    "hub-kill": ("takeover_s", "first_settle_s"),
}


def measured(window: dict) -> bool:
    return all(window["reclaim"][name] is not None for name in MEASURED[window["fault"]])


def window(records: list[dict], fault: dict, until: int) -> dict:
    runs = executions(records)
    seen = [record for record in records if record["t"] <= until]
    lost = orphans(seen, runs, fault["t"], until)
    doubled = duplicates(runs, fault["t"], until)
    handled = settles(seen, fault["t"], until)
    return {
        "fault": fault["fault"],
        "target": fault.get("target"),
        "window_s": _seconds(fault["t"], until),
        "duplicate_claims": len(doubled),
        "duplicates": doubled,
        "orphans": len(lost),
        "orphaned": lost,
        "exit_events_handled_twice": len(handled["handled_twice"]),
        "exit_events_never_handled": len(handled["never_handled"]),
        "reclaim": RECLAIM[fault["fault"]](records, fault, until),
    }


def report(records: list[dict]) -> dict:
    faults = [r for r in records if r["kind"] == "fault"]
    ends = {r["fault"]: r["t"] for r in records if r["kind"] == "window-end"}
    windows = [window(records, fault, ends.get(fault["fault"], records[-1]["t"])) for fault in faults]
    final_at = records[-1]["t"]
    runs = executions(records)
    lost = orphans(records, runs, final_at, final_at)
    handled = settles(records, -math.inf, final_at + SETTLE_GRACE_MS)
    final = {
        "offered": sum(1 for r in records if r["kind"] == "offered"),
        "launches": len(runs),
        "duplicate_claims": len(duplicates(runs)),
        "orphans": len(lost),
        "orphaned": lost,
        "exit_events_handled_twice": len(handled["handled_twice"]),
        "exit_events_never_handled": len(handled["never_handled"]),
    }
    clean = len(windows) == len(MEASURED) and all(
        not (w["duplicate_claims"] or w["orphans"] or w["exit_events_never_handled"]) and measured(w) for w in windows
    )
    unsettled = final["exit_events_never_handled"]
    return {
        "windows": windows,
        "final": final,
        "contract_met": clean and not (unsettled or final["duplicate_claims"] or lost),
    }
