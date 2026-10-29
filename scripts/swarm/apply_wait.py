"""Hold a Doctor tune task's claim until the Doctor records applying each code fix it measures."""

from collections.abc import Callable

from scripts.swarm_ledger import task_dependencies

FIELD = "applied_at"
DOCTOR = "doctor"


def unapplied(task: dict, rows: dict) -> list[str]:
    deps = task.get("depends_on") or []
    return [d for d in deps if task_dependencies.measures(task, rows.get(d, {})) and not rows.get(d, {}).get(FIELD)]


def waiting(config: object, task: dict, rows: dict) -> list[str]:
    return unapplied(task, rows) if config.template == DOCTOR else []


def checker(config: object, rows: dict) -> Callable[[dict], bool]:
    return lambda task: not waiting(config, task, rows)
