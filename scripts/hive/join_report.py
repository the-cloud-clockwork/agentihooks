import json
from typing import TYPE_CHECKING

from scripts.hive import registry

if TYPE_CHECKING:
    from redis import Redis

    from scripts.swarm.store import AgentRecord

REQUIRED = (
    "ledger_join",
    "ledger_write",
    "redis_claim",
    "redis_release",
    "redis_status",
    "redis_verdict",
    "transcript",
    "github",
    "profile",
    "settings",
    "pane",
    "started_at",
)
OPTIONAL = ("brain",)
HEARTBEAT_MS = 180_000
GONE = "agent gone from the swarm"


def key(hive: str) -> str:
    return f"{registry.key(hive)}:join-reports"


def graded(report: dict) -> dict:
    checks = report["checks"]
    misses = {name: {"required": name in REQUIRED} for name in (*REQUIRED, *OPTIONAL) if checks.get(name) is not True}
    grade = "failed" if any(miss["required"] for miss in misses.values()) else "partial" if misses else "complete"
    return {**report, "grade": grade, "misses": misses}


def summary(report: dict) -> str:
    misses = ", ".join(
        f"{name} ({'required' if miss['required'] else 'optional'})" for name, miss in report["misses"].items()
    )
    line = f"{report['agent']}\t{report['grade']}\tmisses={misses or 'none'}\theartbeat={report['heartbeat_at']}"
    if "ended_at" in report:
        line += f"\texit={report['exit_reason']}\tstatus={report['status']}"
    return line


def text(redis: "Redis", agents: list["AgentRecord"]) -> str:
    return "".join("hive join  " + summary(report) + "\n" for agent in agents if (report := read(redis, agent)))


def host(agent: "AgentRecord") -> str:
    return agent.hive or agent.runtime_target.get("host_id", "")


def read(redis: "Redis", agent: "AgentRecord") -> dict:
    if not host(agent) or not agent.execution_id:
        return {}
    raw = redis.hget(key(host(agent)), agent.execution_id)
    report = json.loads(raw) if raw else {}
    return report if report.get("generation") == agent.generation else {}


def listing(redis: "Redis", hive: str) -> list[dict]:
    return [json.loads(raw) for raw in redis.hvals(key(hive))]


def start(redis: "Redis", slug: str, agent: "AgentRecord", session: str, checks: dict, context: dict, at: int) -> dict:
    previous = read(redis, agent)
    if previous.get("session_id") == session:
        return previous
    report = graded(
        {
            "agent": agent.name,
            "slug": slug,
            "task": agent.task,
            "execution_id": agent.execution_id,
            "generation": agent.generation,
            "session_id": session,
            "started_at": at,
            "heartbeat_at": at,
            "status": "running",
            "checks": checks,
            "context": context,
        }
    )
    redis.hset(key(host(agent)), agent.execution_id, json.dumps(report))
    return report


def end(redis: "Redis", hive: str, execution: str, session: str, reason: str, status: str | int, at: int) -> dict:
    from redis.exceptions import WatchError

    while True:
        with redis.pipeline() as pipe:
            try:
                pipe.watch(key(hive))
                raw = pipe.hget(key(hive), execution)
                report = json.loads(raw) if raw else {}
                if report.get("session_id") != session:
                    return {}
                report = {**report, "ended_at": at, "exit_reason": reason, "status": status}
                pipe.multi()
                pipe.hset(key(hive), execution, json.dumps(report))
                pipe.execute()
                return report
            except WatchError:
                continue


def cheap_checks(redis: "Redis", report: dict) -> dict:
    from scripts.hive.join_checks import cheap

    return cheap(redis, report)


def live(redis: "Redis", reports: list[dict]) -> set[tuple[str, str, int]]:
    from scripts.swarm.store import RedisStore

    store = RedisStore(redis)
    return {
        (slug, agent.execution_id, agent.generation)
        for slug in {report["slug"] for report in reports}
        for agent in store.agents(slug)
    }


def heartbeat(redis: "Redis", hive: str, at: int) -> None:
    from redis.exceptions import WatchError

    reports = [report for report in listing(redis, hive) if report["status"] == "running"]
    agents = live(redis, reports)
    for report in reports:
        if (report["slug"], report["execution_id"], report["generation"]) not in agents:
            end(redis, hive, report["execution_id"], report["session_id"], GONE, "ended", at)
            continue
        if at - report["heartbeat_at"] < HEARTBEAT_MS:
            continue
        checks = cheap_checks(redis, report)
        with redis.pipeline() as pipe:
            try:
                pipe.watch(key(hive))
                raw = pipe.hget(key(hive), report["execution_id"])
                current = json.loads(raw) if raw else {}
                if current != report:
                    continue
                updated = graded({**report, "checks": {**report["checks"], **checks}, "heartbeat_at": at})
                pipe.multi()
                pipe.hset(key(hive), report["execution_id"], json.dumps(updated))
                pipe.execute()
            except WatchError:
                continue
