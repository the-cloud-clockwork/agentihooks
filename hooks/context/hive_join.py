import os
import time
from pathlib import Path


def run(payload: dict, event: str) -> None:
    slug = os.environ.get("AGENTIHOOKS_SWARM")
    name = os.environ.get("AGENTIHOOKS_AGENT_NAME")
    if not (slug and name):
        return
    from hooks.targets import current_target
    from scripts.hive import join_checks, join_report
    from scripts.profiles.binding import HOMES
    from scripts.swarm.commands import hive_id
    from scripts.swarm.store import connect

    hive = os.environ.get("AGENTIHOOKS_HIVE_ID") or hive_id()
    store = connect()
    session = payload.get("session_id", "")
    at = int(time.time() * 1000)
    if event == "end":
        for report in join_report.listing(store.redis, hive):
            if report["session_id"] == session:
                join_report.end(
                    store.redis,
                    hive,
                    report["execution_id"],
                    session,
                    payload.get("reason", "unknown"),
                    payload.get("exit_status", "ended"),
                    at,
                )
        return
    agent = next((a for a in store.agents(slug) if a.name == name and join_report.host(a) == hive), None)
    if agent is None:
        return
    if event == "tool":
        join_report.retry_ledger(store.redis, agent, session, at)
        return
    if join_report.read(store.redis, agent).get("session_id") == session:
        return
    transcript = payload.get("transcript_path")
    harness = agent.harness or current_target()
    context = {
        "home": os.environ.get(HOMES.get(harness, ""), ""),
        "profile": agent.profile or os.environ.get("AGENTIHOOKS_PROFILE"),
        "harness": harness,
        "pane": os.environ.get("HERDR_PANE_ID", agent.pane_id),
        "transcript_dir": str(Path(transcript).parent) if transcript else "",
        "headed": join_checks.headed(store.redis, hive),
    }
    report = {"agent": name, "slug": slug, "execution_id": agent.execution_id, "started_at": at, "context": context}
    checks = join_checks.startup(store.redis, report)
    join_report.start(store.redis, slug, agent, session, checks, context, at)
