import os
import shutil
import subprocess

from scripts.swarm import commands
from scripts.swarm.status import status_report
from scripts.swarm.store import RedisStore
from scripts.swarm_ledger import ledger_workspace


def run(argv: list[str], env: dict) -> str:
    exe = shutil.which("agentihooks")
    if not exe:
        return "agentihooks is not on PATH"
    try:
        done = subprocess.run([exe, *argv], capture_output=True, text=True, timeout=60, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        return str(exc)
    return "" if done.returncode == 0 else (done.stderr or done.stdout).strip() or "swarm command failed"


def execute(store: RedisStore, slug: str, row: dict) -> str:
    command, argv = row["command"], row["argv"]
    env = {**os.environ, "AGENTIHOOKS_AGENT_NAME": "operator", "AGENTIHOOKS_CONTROL_SOURCE": "page"}
    if command == "quota":
        from scripts import agents_quota

        return agents_quota.refresh_page_quota(lambda: run(["quota", "--refresh", "--json"], env))
    if command == "swarm" and argv[0] == "terminate":
        if not any(agent.name == argv[1] for agent in store.agents(slug)):
            return "agent is not in this swarm"
        target = ["terminate-agent", argv[1], "--type", "any"]
        return run([*target, "--dry-run"], env) or run(target, env)
    if command not in {"swarm", "doctor"}:
        return "unknown control command"
    return run([command, slug, *argv], env)


def consume(store: RedisStore, slug: str) -> list[str]:
    return commands.consume(store, slug, commands.hive_id(), lambda row: execute(store, slug, row))


def publish(store: RedisStore, slug: str, ledger: dict) -> None:
    tails = {
        task["id"]: ledger_workspace.tails(slug, task["id"])
        for task in ledger.get("tasks", [])
        if task.get("workspace")
    }
    commands.publish(store, slug, status_report(store, slug, ledger), tails)
