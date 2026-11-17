"""Master drill: retire the front door master of a scratch proof swarm, leaving its seat empty, and record the
successor the tick's enforcing night watch forces up within thirty minutes of a drill clock."""

from typing import Any

from scripts.swarm.night_watch_drill import Drill

KEY = "master-drill"
NOT_SCRATCH = "{slug} is not a scratch proof swarm; the master drill never runs on a live swarm"
NOT_RUNNING = "scratch swarm {slug} is {state}; the master drill runs on a running or paused scratch swarm"
NOT_RETIRED = (
    "the runtime could not retire the front door master {name} of {slug}; the master drill needs an empty seat"
)


def run(drill: Drill) -> dict[str, Any]:
    """The drill record of ``drill``."""
    return {"swarm": drill.slug}
