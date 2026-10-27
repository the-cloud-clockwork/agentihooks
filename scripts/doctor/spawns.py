import json
import re

from scripts.hive import admission, placement
from scripts.swarm.health.findings import Finding

TICK_MS = 60_000
FAILURE = re.compile(r"^(?:spawn failed for ([^\s,:]+).*?|master spawn failed): (.+)$")
HOST_ROOM = re.compile(
    rf"no (?:{placement.HIVE}|{placement.HEADLESS}|{placement.UI_HIVE}) for \S+ {placement.HAS_ROOM}: (.+)"
)


def _host_room_refusal(reason: str) -> bool:
    match = HOST_ROOM.fullmatch(reason.partition(placement.LOCAL_FULL)[0])
    return bool(match) and all(
        admission.category(part) in (admission.CPU, admission.RAM) and admission.LINE in part
        for part in match[1].split("; ")
    )


def _reservation_room(snapshot: dict) -> bool:
    return any(
        row["harness"] == snapshot.get("harness")
        and row["state"] == "OPEN"
        and row.get("cap") is not None
        and row["sessions"] < row["cap"]
        for row in snapshot.get("accounts", [])
    )


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
            if "account reservation is full" in match[2] and not _reservation_room(snapshot):
                continue
            if _host_room_refusal(match[2]):
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


def silent_starts(
    agents: list[dict], hooked: dict, now_ms: int, beats: dict | None = None, pending: frozenset = frozenset()
) -> list[Finding]:
    beats = beats or {}
    found = []
    for agent in agents:
        if agent.get("state") == "finished" or agent["name"] in pending:
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


def findings(record: dict) -> list[Finding]:
    return [*failed(record), *fresh_restores(record)]
