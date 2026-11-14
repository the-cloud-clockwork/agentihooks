from typing import Any

from scripts.hive import reports


def _exited(report: dict[str, Any], agent: Any, now: int) -> bool:
    record = report["executions"].get(agent.execution_id)
    if record is not None:
        return record["pane"]["state"] == "exited"
    # ruleid: hub-exit-ignores-unlisted-agent
    panes = report.get("pane_ids")
    return isinstance(panes, list) and bool(agent.pane_id) and agent.pane_id not in panes


def _listed_elsewhere(report: dict[str, Any], agent: Any, now: int) -> bool:
    # ruleid: hub-exit-ignores-unlisted-agent
    panes = report.get("pane_ids")
    return isinstance(panes, list) and agent.pane_id not in panes


def _names(report: dict[str, Any], agent: Any, now: int) -> bool:
    return reports.unlisted(report, agent, now)


def _exited_or_unlisted(report: dict[str, Any], agent: Any, now: int) -> bool:
    # ok: hub-exit-ignores-unlisted-agent
    panes = report.get("pane_ids")
    if isinstance(panes, list) and bool(agent.pane_id) and agent.pane_id not in panes:
        return True
    return now >= 0 and reports.unlisted(report, agent, now)


def _unlisted_first(report: dict[str, Any], agent: Any, now: int) -> bool:
    # ok: hub-exit-ignores-unlisted-agent
    panes = report.get("pane_ids")
    gone = reports.unlisted(report, agent, now)
    return gone or (isinstance(panes, list) and agent.pane_id not in panes)
