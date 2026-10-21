"""Each hive's report of the executions it runs, published every heartbeat and read by the controller for workers on
other hives; a hive that stopped beating has no report."""

import json
from typing import TYPE_CHECKING

from scripts.hive import registry

if TYPE_CHECKING:
    from redis import Redis

    from scripts.swarm.store import AgentRecord


def key(hive: str) -> str:
    return f"{registry.key(hive)}:executions"


def host(agent: "AgentRecord") -> str:
    from scripts.swarm.commands import hive_id

    target = agent.runtime_target.get("host_id") if agent.runtime_backend == "local" else ""
    return target if target and target != hive_id() else ""


def pane_key(agent: "AgentRecord") -> str:
    remote = host(agent)
    return f"{remote}/{agent.pane_id}" if remote else agent.pane_id


class Reports:
    def __init__(self, redis: "Redis") -> None:
        self.redis = redis

    def read(self, hive: str) -> dict | None:
        raw = self.redis.get(key(hive))
        return json.loads(raw) if raw else None

    def live(self) -> dict[str, dict]:
        return {hive: report for hive in sorted(self.redis.smembers(registry.INDEX)) if (report := self.read(hive))}

    def execution(self, agent: "AgentRecord") -> dict | None:
        report = self.read(host(agent))
        if report is None:
            return None
        found = report["executions"].get(agent.execution_id, {})
        return found if found.get("generation") == agent.generation else {}

    def gone(self, agent: "AgentRecord") -> bool:
        report = self.read(host(agent))
        if report is None:
            return False
        if report.get("observed_at", 0) < (agent.launched_at or agent.started_at):
            return False
        panes = report.get("pane_ids")
        if not isinstance(panes, list):
            return False
        namespace = report.get("process_namespace")
        launched = agent.runtime_target.get("process_namespace")
        if namespace and launched and namespace != launched:
            return True
        return bool(agent.pane_id) and agent.pane_id not in panes

    def names(self) -> set[str]:
        return {name for report in self.live().values() for name in report["names"]}

    def holder(self, name: str) -> str:
        return next((hive for hive, report in self.live().items() if name in report["names"]), "")

    def conversations(self) -> dict[str, str]:
        return {
            f"{hive}/{pane}": conversation
            for hive, report in self.live().items()
            for pane, conversation in report["panes"].items()
        }
