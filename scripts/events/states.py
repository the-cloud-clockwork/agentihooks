TASK: dict[str, tuple[str, ...]] = {}
EXECUTION: dict[str, tuple[str, ...]] = {}
HIVE: dict[str, tuple[str, ...]] = {}
SLOT: dict[str, tuple[str, ...]] = {}
MACHINES: dict[str, dict[str, tuple[str, ...]]] = {}


class IllegalTransition(Exception):
    pass


def transition(machine: str, current: str, target: str) -> str:
    raise NotImplementedError
