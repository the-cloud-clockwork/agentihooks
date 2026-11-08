"""Plans MCP category: plan_ingest puts a plan file or inline text into a ledger, as agentihooks plan ingest does."""

import json
import os
import sys
import tempfile
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

from hooks.mcp._session import env_fallback_allowed

PLANNING = ("", "auto", "manual")
NETWORK = "plan_ingest runs only under stdio: a network transport serves every session and cannot name the author"


def run(path: str, text: str, ledger: str, planning: str, dry_run: bool) -> dict[str, Any]:
    """One ingest as the tool's JSON: success, plan, phases and the swarm state, or success false with the refusal."""
    if not env_fallback_allowed():
        return {"success": False, "error": NETWORK}
    if bool(path) == bool(text):
        return {"success": False, "error": "pass exactly one of path or text"}
    if path == "-":
        return {"success": False, "error": "standard input is the MCP channel; pass the plan as text"}
    if planning not in PLANNING:
        return {"success": False, "error": "planning is auto, manual or empty"}
    from scripts.plans import ingest

    with tempfile.TemporaryDirectory() as folder:
        source = path
        if text:
            source = str(Path(folder) / "inline.md")
            Path(source).write_text(text, encoding="utf-8")
        args = Namespace(path=source, ledger=ledger, planning=planning, dry_run=dry_run, repo="")
        try:
            with redirect_stdout(sys.stderr):
                result = ingest.ingest(args, os.environ)
        except (ingest.IngestError, SystemExit, OSError) as exc:
            return {"success": False, "error": str(exc)}
    state = result.pop("swarm", "dry run")
    return {"success": True, **result, "state": state}


def register(mcp: Any) -> None:
    """Register the plan_ingest tool."""

    @mcp.tool()
    def plan_ingest(ledger: str, path: str = "", text: str = "", planning: str = "", dry_run: bool = False) -> str:
        """Put a plan into a ledger as linked phases and slices in one atomic batch; a rerun reports already ingested.

        Args:
            ledger: the target ledger slug.
            path: a plan file: init-swarm JSON, markdown with phase markers, or any markdown.
            text: the plan as inline text, instead of path.
            planning: auto or manual for every phase; empty keeps each phase's own.
            dry_run: report what would be written and write nothing.
        """
        return json.dumps(run(path, text, ledger, planning, dry_run))
