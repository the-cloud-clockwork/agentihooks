from typing import Any


# ruleid: lost-reap-drops-after-refused-retire
def lost_resume(runtime: Any, tick: Any, agent: Any) -> list[str]:
    runtime.retire(agent, homes=tick.reaper.scratch_homes("sw", agent.task))
    return [f"lost {agent.name}" + tick._drop("sw", agent)]


# ruleid: lost-reap-drops-after-refused-retire
def lost_deliberate(runtime: Any, tick: Any, agent: Any) -> list[str]:
    if tick.deliberate_exit.take("sw", agent.name):
        runtime.retire(agent, homes=tick.reaper.scratch_homes("sw", agent.task))
        return [f"retired {agent.name}" + tick._drop("sw", agent, refund=True)]
    return []


# ok: lost-reap-drops-after-refused-retire
def checked_lost(runtime: Any, tick: Any, agent: Any) -> list[str]:
    if not runtime.retire(agent, homes=tick.reaper.scratch_homes("sw", agent.task)):
        return []
    return [f"lost {agent.name}" + tick._drop("sw", agent)]


# ok: lost-reap-drops-after-refused-retire
def checked_deliberate(runtime: Any, tick: Any, agent: Any) -> list[str]:
    if tick.deliberate_exit.take("sw", agent.name):
        if not runtime.retire(agent, homes=tick.reaper.scratch_homes("sw", agent.task)):
            return []
        return [f"retired {agent.name}" + tick._drop("sw", agent, refund=True)]
    return []
