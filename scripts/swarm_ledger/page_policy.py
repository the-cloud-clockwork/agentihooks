"""The page security policy, with the Impeccable live origin added only on an opted in scratch server."""

from pathlib import Path

LIVE_SETTING = "LEDGER_IMPECCABLE_LIVE"
LIVE_ORIGIN = "http://localhost:8400"
SHARED_PORT = 8765


def scratch(folder: Path, port: int) -> bool:
    return port != SHARED_PORT and folder.resolve() != (Path.home() / "development-ledger").resolve()


def policy(environ, folder: Path, port: int) -> str:
    live = f" {LIVE_ORIGIN}" if environ.get(LIVE_SETTING) == "1" and scratch(folder, port) else ""
    return (
        f"default-src 'none'; script-src 'self'{live}; style-src 'self'; img-src 'self'; connect-src 'self'{live}; "
        "base-uri 'none'; form-action 'self'; frame-ancestors 'none'; object-src 'none'"
    )
