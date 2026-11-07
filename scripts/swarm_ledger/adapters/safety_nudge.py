"""Where the ledger records a master joining as orchestrator in the host registry the safety nudge reads."""

from collections.abc import Mapping


def record_master(environ: Mapping[str, str]) -> None:
    """Record this session in the safety nudge registry when it is a swarm master in a herdr pane."""
    from scripts.swarm import safety_nudge

    safety_nudge.record(environ)
