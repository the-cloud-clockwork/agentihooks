from typing import Any


def live_agents(agents: list[Any], runtime: Any) -> list[Any]:
    names = runtime.live_names()
    # ruleid: status-live-agents-ignores-hive
    return [a for a in agents if a.runtime_backend != "local" or reports.host(a) or a.name in names]


def live_agents(agents: list[Any], runtime: Any) -> list[Any]:
    names, here = runtime.live_names(), hive_id()
    # ok: status-live-agents-ignores-hive
    return [
        a
        for a in agents
        if a.runtime_backend != "local" or reports.host(a) or (a.hive and a.hive != here) or a.name in names
    ]


def remote(agents: list[Any]) -> list[Any]:
    # ok: status-live-agents-ignores-hive
    return [a for a in agents if reports.host(a)]
