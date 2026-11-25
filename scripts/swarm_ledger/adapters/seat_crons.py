"""Where the ledger asks the swarm to keep the master seat crons when a master joins as orchestrator."""


def ensure_master(slug: str, name: str) -> None:
    from scripts.swarm import seat_crons

    seat_crons.on_join(slug, name)
