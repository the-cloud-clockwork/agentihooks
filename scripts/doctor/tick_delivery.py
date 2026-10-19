"""The scheduled swarm tick on this machine, flagged while no fresh delivery from the installed code proves it."""

import socket

from scripts.swarm.health.findings import Finding
from scripts.swarm.scheduled_tick import FRESH_S

KIND = "scheduled tick"


def findings(why: str, machine: str) -> list[Finding]:
    if not why:
        return []
    return [
        Finding(
            KIND,
            machine,
            "the scheduler has not proven the swarm tick, so the systemd timer stays",
            (why,),
            f"a swarm tick delivered by the installed code within the last {FRESH_S} seconds",
            1,
        )
    ]


def read() -> list[Finding]:
    from scripts.swarm import timer

    return findings(timer.undelivered(), socket.gethostname())
