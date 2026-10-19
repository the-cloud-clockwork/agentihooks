"""The interfaces the ledger owns for what it needs from the swarm, the hive and the hooks."""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Swarm(Protocol):
    naming: Any
    store: Any
    commands: Any
    lifecycle: Any
    freeze: Any
    effort_range: Any
    claim_hold: Any
    idle: Any
    lease: Any
    model_catalog: Any
    default_models: Any
    quota_refresh: Any
    operator_mail: Any
    overlays: Any
    verdicts: Any
    phase_state: Any
    affinity: Any
    templates: Any
    stack_env: Any
    stack_init: Any
    front_door: Any
    ledger_writer: Any
    master_fence: Any
    masters: Any
    architecture: Any
    operator_auth: Any
    task_specs: Any
    authority: Any
    ledger_cutover: Any


@runtime_checkable
class Hive(Protocol):
    auth: Any
    registry: Any
    launch: Any
    reports: Any


@runtime_checkable
class Hooks(Protocol):
    config: Any
    conditions: Any
    filters: Any
    operator_words: Any
    classifier: Any
    profile_chain: Any

    def redis(self) -> Any: ...


BOUND: dict[str, object] = {}


# API modules build their schemas from port constants at import, so bind before importing them.
def bind(**adapters: object) -> None:
    BOUND.update(adapters)


def _port(name: str) -> Any:
    if name not in BOUND:
        from scripts.swarm_ledger import composition

        for key, adapter in composition.adapters().items():
            BOUND.setdefault(key, adapter)
    return BOUND[name]


def swarm() -> Swarm:
    return _port("swarm")


def hive() -> Hive:
    return _port("hive")


def hooks() -> Hooks:
    return _port("hooks")
