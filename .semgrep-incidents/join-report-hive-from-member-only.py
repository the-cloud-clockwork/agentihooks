import os

from scripts.hive import join_report
from scripts.swarm.commands import hive_id


def run(redis: object, slug: str) -> None:
    # ruleid: join-report-hive-from-member-only
    hive = os.environ.get("AGENTIHOOKS_HIVE_ID")
    if not hive:
        return
    join_report.listing(redis, hive)


def run_with_root(redis: object, slug: str) -> None:
    # ok: join-report-hive-from-member-only
    hive = os.environ.get("AGENTIHOOKS_HIVE_ID") or hive_id()
    join_report.listing(redis, hive)


def member_only(environ: dict) -> str:
    # ok: join-report-hive-from-member-only
    own = os.environ.get("AGENTIHOOKS_HIVE_ID")
    return own or ""
