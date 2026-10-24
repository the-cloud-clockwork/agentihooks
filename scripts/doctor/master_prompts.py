"""Watched master panes held on an input prompt: one finding per seat once the wait reaches fifteen minutes. Its
measure and evidence move only in fifteen minute steps, so a judged finding returns when the wait grows by another
step. The Doctor only reads the wait; it never types into the pane."""

import time

from scripts.swarm.health.findings import Finding

KIND = "master waiting on input"
STEP_MINUTES = 15
MINUTE_MS = 60_000


def _iso(ms):
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(ms / 1000))


def findings(agents: list[dict], since: dict[str, int | None], now_ms: int) -> list[Finding]:
    found = []
    for agent in agents:
        entered = since.get(agent["name"])
        if agent["lane"] != "master" or not agent["input_prompt"] or not entered:
            continue
        waited = (now_ms - entered) // MINUTE_MS // STEP_MINUTES * STEP_MINUTES
        if not waited:
            continue
        seat = agent["seat"] or agent["name"]
        found.append(
            Finding(
                KIND,
                seat,
                f"the master pane has waited on an input prompt for {waited} minutes",
                (
                    f"seat {seat}",
                    f"prompt: {agent['input_prompt']}",
                    f"waited {waited} minutes since {_iso(entered)}",
                    f"agent {agent['name']}",
                ),
                f"a master pane on one input prompt for {STEP_MINUTES} minutes, again each further {STEP_MINUTES} minutes",
                waited,
            )
        )
    return found
