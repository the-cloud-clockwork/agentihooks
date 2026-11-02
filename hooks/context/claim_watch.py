"""A swarm worker whose task claim no longer names it ends at its next tool call: it leaves a work note, starts its own
swarm exit and is denied the call."""

import os
import subprocess
import time
from collections.abc import Callable, Mapping

from hooks.context import worker_mode
from scripts.swarm.claim_lost import lost

WORKERS = frozenset({"eng", "ci"})
STOP = (
    "STOP: task {task} on swarm {slug} is no longer claimed by you; its claim names {owner}. This session is ending: "
    "run no other tool and end your turn now."
)
NOTE = "{at} {name} ended at its next tool call: the task claim names {owner}\n"
READ_TIMEOUT = 2


def check(
    environ: Mapping[str, str] | None = None,
    ledger=None,
    now: float | None = None,
    spawn: Callable | None = None,
) -> str | None:
    from scripts.hive import scope
    from scripts.swarm_ledger import ledger_workspace

    env = os.environ if environ is None else environ
    slug, name, task = (env.get(k) for k in ("AGENTIHOOKS_SWARM", "AGENTIHOOKS_AGENT_NAME", "AGENTIHOOKS_SWARM_TASK"))
    if env.get("AGENTIHOOKS_SWARM_LANE") not in WORKERS or not (slug and name and task) or worker_mode.active(env):
        return None
    now = time.time() if now is None else now
    folder = ledger_workspace.folder(slug, task)
    folder.mkdir(parents=True, exist_ok=True)
    if ledger is None:
        from scripts.swarm.ledger_client import LedgerClient

        ledger = LedgerClient(read_timeout=READ_TIMEOUT)
    row = ledger.task(slug, task)
    if not lost(name, row, bool(scope.member(env))):
        return None
    owner = row.get("claimed_by") or "nobody"
    at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    with (folder / "progress.md").open("a") as progress:
        progress.write(NOTE.format(at=at, name=name, owner=owner))
    (spawn or subprocess.Popen)(
        ["agentihooks", "swarm", slug, "--as", name, "exit"],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return STOP.format(task=task, slug=slug, owner=owner)
