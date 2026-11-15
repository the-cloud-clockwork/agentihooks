from typing import Any


def heartbeat(redis: Any, hive: str, at: int) -> None:
    reports = [report for report in listing(redis, hive) if report["status"] == "running"]
    for report in end_gone(redis, hive, reports, at):
        checks = held_checks(redis, report)
        # ruleid: hive-heartbeat-refresh-ignores-restart
        updated = graded({**report, "checks": {**report["checks"], **checks}, "heartbeat_at": at})
        redis.hset(key(hive), report["execution_id"], json.dumps(updated))


def heartbeat_discarding_restart(redis: Any, hive: str, at: int) -> None:
    reports = [report for report in listing(redis, hive) if report["status"] == "running"]
    end_restarted(redis, hive, reports, at)
    for report in end_gone(redis, hive, reports, at):
        # ruleid: hive-heartbeat-refresh-ignores-restart
        updated = graded({**report, "heartbeat_at": at})
        redis.hset(key(hive), report["execution_id"], json.dumps(updated))


def heartbeat_after_restart(redis: Any, hive: str, at: int) -> None:
    reports = [report for report in listing(redis, hive) if report["status"] == "running"]
    for report in end_gone(redis, hive, end_restarted(redis, hive, reports, at), at):
        checks = held_checks(redis, report)
        # ok: hive-heartbeat-refresh-ignores-restart
        updated = graded({**report, "checks": {**report["checks"], **checks}, "heartbeat_at": at})
        redis.hset(key(hive), report["execution_id"], json.dumps(updated))


def start(redis: Any, agent: Any, session: str, checks: dict, at: int) -> None:
    # ok: hive-heartbeat-refresh-ignores-restart
    report = graded({"agent": agent.name, "session_id": session, "started_at": at, "heartbeat_at": at, "checks": checks})
    redis.hset(key(agent.hive), agent.execution_id, json.dumps(report))
