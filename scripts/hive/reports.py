"""Each hive's report of the executions it runs, published every heartbeat and read by the controller for workers on
other hives; a hive that stopped beating has no report."""

import json
from typing import TYPE_CHECKING, Any, cast

from scripts.hive import registry
from scripts.hive.auth import HiveError

if TYPE_CHECKING:
    from redis import Redis

    from scripts.swarm.store import AgentRecord

LOST_MS = 10 * 60_000
STARTUP_MS = 6 * 60_000


def key(hive: str) -> str:
    return f"{registry.key(hive)}:executions"


def host(agent: "AgentRecord") -> str:
    from scripts.swarm.commands import hive_id

    target = agent.runtime_target.get("host_id") if agent.runtime_backend == "local" else ""
    return target if target and target != hive_id() else ""


def pane_key(agent: "AgentRecord") -> str:
    remote = host(agent)
    return f"{remote}/{agent.pane_id}" if remote else agent.pane_id


def unlisted(report: dict[str, Any], agent: "AgentRecord", launched: int) -> bool:
    """True when ``report`` no longer names ``agent`` while it names others or its pod started after ``launched``."""
    found: object = report.get("names")
    if not isinstance(found, list):
        return False
    names = cast("list[object]", found)
    return agent.name not in names and (bool(names) or report.get("started_at", 0) > launched)


class Reports:
    def __init__(self, redis: "Redis") -> None:
        self.redis = redis

    def read(self, hive: str) -> dict | None:
        raw = self.redis.get(key(hive))
        return json.loads(raw) if raw else None

    def live(self) -> dict[str, dict]:
        return {hive: report for hive in registry.hive_state.index(self.redis) if (report := self.read(hive))}

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
        launched = agent.launched_at or agent.started_at
        observed = report.get("observed_at", 0)
        if observed < launched:
            return False
        if report.get("started_at", 0) > launched:
            return True
        names = report.get("names")
        if isinstance(names, list) and names and agent.name not in names and observed - launched >= STARTUP_MS:
            return True
        panes = report.get("pane_ids")
        if not isinstance(panes, list):
            return False
        namespace = report.get("process_namespace")
        recorded = agent.runtime_target.get("process_namespace")
        if namespace and recorded and namespace != recorded:
            return True
        return bool(agent.pane_id) and agent.pane_id not in panes

    def vanished(self, agent: "AgentRecord", now_ms: int) -> bool:
        """True past startup when the beating hive that admitted ``agent`` no longer lists it, while it lists others
        or its pod started after the launch, or when that hive left the hive index without a live heartbeat."""
        raw = self.redis.get(key(agent.hive)) if agent.hive else None
        launched = agent.launched_at or agent.started_at
        if not agent.hive or not launched or now_ms - launched < STARTUP_MS:
            return False
        if not raw:
            return self._unlisted(agent.hive, now_ms)
        return unlisted(json.loads(cast(str, raw)), agent, launched)

    def _unlisted(self, hive: str, now_ms: int) -> bool:
        """True when ``hive`` is out of the hive index and its record is gone or no longer beating."""
        if hive in registry.hive_state.index(self.redis):  # pyright: ignore[reportUnknownMemberType]
            return False
        try:
            record = registry.show(self.redis, hive)
        except HiveError:
            return False
        return record is None or not registry.live(record, now_ms)

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
            hive in registry.hive_state.index(self.redis)
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

    def executions_of(self, hive: str, name: str) -> list[str]:
        """Each execution the hive reports under the name, as its execution id and generation, sorted."""
        raw = self.redis.get(key(hive))
        if not raw:
            return []
        executions: dict[str, dict[str, object]] = json.loads(raw)["executions"]
        return sorted(
            f"{execution}:{entry['generation']}" for execution, entry in executions.items() if entry["name"] == name
        )

    def conversations(self) -> dict[str, str]:
        return {
            f"{hive}/{pane}": conversation
            for hive, report in self.live().items()
            for pane, conversation in report["panes"].items()
        }
