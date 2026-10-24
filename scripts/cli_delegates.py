import importlib
from collections.abc import Callable

DELEGATED_CLIS = {
    "msg": "scripts.inbox.cli",
    "recall": "scripts.recall.cli",
    "trace": "scripts.trace_cli",
    "deps": "scripts.deps_preflight",
    "quota": "scripts.agents_quota",
    "plan": "scripts.swarm_ledger.plan_read",
    "hive": "scripts.hive.cli",
    "hub": "scripts.hub.runner",
    "github-webhook": "scripts.hub.github_webhooks",
    "github-app": "scripts.github_app_cli",
    "swarm-terminal": "scripts.swarm_v2.herdr.operator_setup",
    "swarm-jump": "scripts.swarm_v2.kubernetes.jump",
    "accounts": "scripts.account_env",
    "access": "scripts.access_env",
    "timer": "scripts.scheduler.cli:timer",
    "stopwatch": "scripts.scheduler.cli:stopwatch",
    "cron": "scripts.scheduler.cli:cron",
}


def delegated_cli(argv: list[str]) -> Callable[[list[str]], int] | None:
    for name in argv[:1]:
        if name in DELEGATED_CLIS:
            module, _, function = DELEGATED_CLIS[name].partition(":")
            return getattr(importlib.import_module(module), function or "main")
