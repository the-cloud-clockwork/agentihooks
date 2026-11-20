from typing import Any


def _synced(reports: Any, agent: Any) -> str:
    # ruleid: controller-folder-host-only
    return reports.host(agent)


def _synced(reports: Any, agent: Any) -> str:
    # ok: controller-folder-host-only
    return reports.host(agent) or reports.admitted(agent)
