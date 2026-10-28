"""The page security policy, with the Impeccable live origin added only on an opted in scratch server."""

from collections.abc import Mapping
from pathlib import Path

LIVE_SETTING = "LEDGER_IMPECCABLE_LIVE"
LIVE_ORIGIN = "http://localhost:8400"
SHARED_PORT = 8765


def scratch(folder: Path, port: int) -> bool:
    return port != SHARED_PORT and folder.resolve() != (Path.home() / "development-ledger").resolve()


def policy(environ: Mapping[str, str], folder: Path, port: int) -> str:
    on = environ.get(LIVE_SETTING) == "1" and scratch(folder, port)
    live = f" {LIVE_ORIGIN}" if on else ""
    styles = f" 'unsafe-inline'{live}" if on else ""
    return (
        f"default-src 'none'; script-src 'self'{live}; style-src 'self'{styles}; img-src 'self'; "
        f"connect-src 'self'{live}; "
        "base-uri 'none'; form-action 'self'; frame-ancestors 'none'; object-src 'none'"
    )
