from typing import Any

from scripts.hive import reports


def wake_target_from_the_host_only(agent: Any) -> str:
    # ruleid: inbox-wake-host-only-target
    return reports.host(agent)


def wake_target_through_the_admitting_hive(agent: Any) -> str:
    # ok: inbox-wake-host-only-target
    return reports.host(agent) or reports.admitted(agent)


def wake_hive_named_in_a_walrus(agent: Any) -> str:
    # ok: inbox-wake-host-only-target
    if host := reports.host(agent) or reports.admitted(agent):
        return host
    return ""
