from scripts.swarm_ledger import ports
from scripts.swarm_ledger.adapters import hive, hooks, presence, swarm


def adapters() -> dict[str, object]:
    return {"swarm": swarm.Swarm(), "hive": hive.Hive(), "hooks": hooks.Hooks(), "presence": presence}


def wire() -> None:
    ports.bind(**adapters())
