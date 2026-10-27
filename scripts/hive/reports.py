"""Each hive's report of the executions it runs, published every heartbeat and read by the controller for workers on
other hives; a hive that stopped beating has no report."""

import json
from typing import TYPE_CHECKING

from scripts.hive import registry
from scripts.hive.auth import HiveError

if TYPE_CHECKING:
    from redis import Redis

    from scripts.swarm.store import AgentRecord

LOST_MS = 10 * 60_000


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

    def lost(self, agent: "AgentRecord", now_ms: int, after_ms: int) -> bool:
        hive = host(agent)
        if not hive or self.read(hive) is not None:
            return False
        try:
            record = registry.show(self.redis, hive)
        except HiveError:
            return False
        return record is not None and 0 < record["heartbeat_at"] < now_ms - after_ms

    def removed(self, agent: "AgentRecord", now_ms: int) -> bool:
        hive = host(agent)
        if agent.state != "finished" or not hive:
            return False
        since_key = f"{registry.key(hive)}:removed-since:{agent.execution_id}:{agent.generation}"
        try:
            record = registry.show(self.redis, hive)
        except HiveError:
            self.redis.delete(since_key)
            return False
        if (
            self.redis.sismember(registry.INDEX, hive)
            or self.read(hive) is not None
            or (record is not None and registry.live(record, now_ms))
        ):
            self.redis.delete(since_key)
            return False
        self.redis.set(since_key, str(now_ms), nx=True)
        return now_ms - int(self.redis.get(since_key)) >= registry.LIVE_MS

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
