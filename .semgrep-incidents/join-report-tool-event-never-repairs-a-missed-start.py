from scripts.hive import join_report


def run(redis: object, agent: object, session: str, at: int, event: str) -> None:
    # ruleid: join-report-tool-event-never-repairs-a-missed-start
    if event == "tool":
        join_report.retry_ledger(redis, agent, session, at)
        return
    join_report.start(redis, "sw", agent, session, {}, {}, at)


def run_with_repair(redis: object, agent: object, session: str, at: int, event: str) -> None:
    current = join_report.read(redis, agent)
    # ok: join-report-tool-event-never-repairs-a-missed-start
    if event == "tool" and current:
        join_report.retry_ledger(redis, agent, session, at)
        return
    if current.get("session_id") == session:
        return
    join_report.start(redis, "sw", agent, session, {}, {}, at)
