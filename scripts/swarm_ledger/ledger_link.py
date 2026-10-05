"""The ledger page link handed to the operator, from the ledger server's configured host and port."""

import os
import urllib.request
from pathlib import Path

START = "agentihooks ledger serve --ensure"


def base() -> str:
    host, port = address()
    return f"http://{host}:{port}"


def shared_directory(directory: Path | None = None) -> bool:
    shared = Path.home() / "development-ledger"
    selected = directory or Path(os.environ.get("LEDGER_DIR", shared)).expanduser()
    return selected.resolve() == shared.resolve()


def address() -> tuple[str, int]:
    port = 8765 if shared_directory() else int(os.environ.get("LEDGER_PORT", "8765"))
    return os.environ.get("LEDGER_HOST", "127.0.0.1"), port


def page_url(slug):
    return f"{base()}/{slug}"


def answering():
    try:
        with urllib.request.urlopen(f"{base()}/healthz", timeout=1):
            return True
    except (OSError, ValueError):
        return False


def page_line(slug):
    line = f"Ledger page: {page_url(slug)} (open it to follow and steer the work)"
    if answering():
        return line
    return f"{line}. The ledger server is not answering: start it with {START}"
