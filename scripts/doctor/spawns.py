import json
import re

from scripts.hive import registry
from scripts.swarm.health.findings import Finding

TICK_MS = 60_000
FAILURE = re.compile(r"^(?:spawn failed for ([^\s,:]+)(?:, [^:]*)?|master spawn failed): (.+)$")
COUNTED = re.compile(r"\bsessions=(?P<sessions>\d+) cap=(?P<cap>\d+)\b")


def _reservation_room(snapshot: dict) -> bool:
    return any(
        row["harness"] == snapshot.get("harness")
        and row["state"] == "OPEN"
        and row.get("cap") is not None
        and row["sessions"] < row["cap"]
        for row in snapshot.get("accounts", [])
    )


def _refusal_room(reason: str, snapshot: dict) -> bool:
    counted = COUNTED.search(reason)
    if counted:
        return int(counted["sessions"]) < int(counted["cap"])
    return _reservation_room(snapshot)


def failed(record: dict) -> list[Finding]:
    failures = {}
    capacity = {}
    prefix = f"{record['slug']}: "
    for action in record["actions"]:
        if not action.startswith(prefix):
            continue
        text = action[len(prefix) :]
        if text.startswith("spawn capacity for "):
            subject, raw = text.removeprefix("spawn capacity for ").split(": ", 1)
            capacity[subject] = json.loads(raw)
            continue
        match = FAILURE.fullmatch(text)
        if match:
            subject = match[1] or "master"
            snapshot = capacity.pop(subject, {})
            if "account reservation is full" in match[2] and not _refusal_room(match[2], snapshot):
                continue
            failures.setdefault(subject, []).append(match[2])
    return [
        Finding(
            "failed spawn",
            subject,
            f"{len(reasons)} failed spawns",
            tuple(reasons),
            "at least one failed spawn",
            len(reasons),
        )
        for subject, reasons in sorted(failures.items())
    ]


def fresh_restores(record: dict) -> list[Finding]:
    return [
        Finding(
            "fresh restore",
            outcome["name"],
            "restore fell back to a fresh session",
            (
                outcome["reason"],
                f"task {outcome['task']}",
                f"conversation {outcome.get('conversation_id', '')}",
                f"at {outcome['at']}",
            ),
            "restore outcome is fresh",
            1,
        )
        for outcome in record["restored"]
        if outcome["outcome"] == "fresh"
    ]


def silent_starts(agents: list[dict], hooked: dict, now_ms: int, beats: dict | None = None) -> list[Finding]:
    beats = beats or {}
    found = []
    for agent in agents:
        if agent.get("state") == "finished":
            continue
        found += _silent_hive(agent, beats, now_ms) if agent.get("hive") else _silent_local(agent, hooked, now_ms)
    return found


def _silent_local(agent: dict, hooked: dict, now_ms: int) -> list[Finding]:
    started = agent.get("started_at")
    if not started or agent["name"] in hooked or now_ms - started < TICK_MS:
        return []
    return [
        Finding(
            "no hook event",
            agent["name"],
            "starting launch has no recorded harness or pane"
            if agent.get("state") == "starting" and not (agent.get("harness") or agent.get("pane_id"))
            else "spawned agent has run no tool since it started",
            (
                f"started at {started}",
                f"harness {agent.get('harness', '')}",
                f"pane {agent.get('pane_id', '')}",
            ),
            "no hook event one tick after spawn",
            (now_ms - started) // TICK_MS,
        )
    ]


def _silent_hive(agent: dict, beats: dict, now_ms: int) -> list[Finding]:
    acked = (agent.get("launch_timings") or {}).get("returned_at") or agent.get("launched_at")
    beat = beats.get(agent["name"])
    if not acked or (beat and beat >= agent["started_at"]) or now_ms - acked < TICK_MS:
        return []
    return [
        Finding(
            "no hook event",
            agent["name"],
            "hive agent has sent no heartbeat since its hive acknowledged the launch",
            (
                f"acknowledged at {acked}",
                f"hive {agent['hive']}",
                f"harness {agent.get('harness', '')}",
                f"pane {agent.get('pane_id', '')}",
            ),
            "no heartbeat one tick after the hive acknowledgement",
            (now_ms - acked) // TICK_MS,
        )
    ]


def missing_headroom(hives: list[dict], readings: dict, now_ms: int) -> list[Finding]:
    return [
        Finding(
            "missing headroom",
            hive["id"],
            "live hive has no fresh headroom reading, so the tick refuses its launches",
            (f"hive {hive['name']}", f"heartbeat at {hive['heartbeat_at']}", f"version {hive.get('version', '')}"),
            "a live worker hive with no fresh headroom reading",
            1,
        )
        for hive in hives
        if hive.get("ui") != "yes" and registry.live(hive, now_ms) and readings.get(hive["id"]) is None
    ]


def findings(record: dict) -> list[Finding]:
    return [*failed(record), *fresh_restores(record)]
