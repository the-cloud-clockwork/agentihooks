"""The ledger page link handed to the operator, from the ledger server's configured host and port."""

import os
import urllib.parse
import urllib.request
from pathlib import Path

START = "agentihooks ledger serve --ensure"
LOOPBACK = ("127.0.0.1", "localhost")


def base() -> str:
    host, port = address()
    return f"http://{host}:{port}"


def shared_directory(directory: Path | None = None, environ=os.environ) -> bool:
    shared = Path.home() / "development-ledger"
    selected = directory or Path(environ.get("LEDGER_DIR", shared)).expanduser()
    return selected.resolve() == shared.resolve()


def address(environ=os.environ) -> tuple[str, int]:
    port = 8765 if shared_directory(environ=environ) else int(environ.get("LEDGER_PORT", "8765"))
    return environ.get("LEDGER_HOST", "127.0.0.1"), port


def listed_hosts(environ=os.environ) -> set[str]:
    public = urllib.parse.urlsplit(environ.get("SWARM_PUBLIC_URL", "")).netloc
    return ({name.strip() for name in environ.get("SWARM_ALLOWED_HOSTS", "").split(",")} | {public}) - {""}


def allowed_hosts(environ=os.environ) -> set[str]:
    host, port = address(environ)
    return {f"{name}:{port}" for name in (host, *LOOPBACK)} | listed_hosts(environ)


def allowed_origins(environ=os.environ) -> set[str]:
    host, port = address(environ)
    loopback = {f"http://{name}:{port}" for name in (host, *LOOPBACK)}
    return loopback | {f"{scheme}://{name}" for name in listed_hosts(environ) for scheme in ("http", "https")}


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
