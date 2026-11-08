"""agentihooks plan: read a task's plan chunk, or ingest a plan into a ledger."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def main(argv: list[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    """Route plan ingest to the ingest command and every other plan command to plan read."""
    import sys

    args = sys.argv[1:] if argv is None else argv
    if args[:1] == ["ingest"]:
        from scripts.plans import ingest

        return ingest.main(args[1:], environ)
    from scripts.swarm_ledger import plan_read

    reader: Any = plan_read
    return reader.main(args, environ)
