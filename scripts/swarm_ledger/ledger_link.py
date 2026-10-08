"""The ledger page link handed to the operator, from the ledger server's configured host and port."""

import http.client
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

START = "agentihooks ledger serve --ensure"
LOOPBACK = ("127.0.0.1", "localhost")


def base(environ=os.environ) -> str:
    host, port = address(environ)
    url = environ.get("LEDGER_URL", "").rstrip("/") if remote(environ) else ""
    return url or f"http://{host}:{port}"


def remote(environ=os.environ) -> bool:
    return environ.get("AGENTIHOOKS_DEPLOYMENT", "local") != "local"


def shared_directory(environ=os.environ) -> bool:
    return folder(environ).resolve() == (Path.home() / "development-ledger").resolve()


def address(environ=os.environ) -> tuple[str, int]:
    port = 8765 if shared_directory(environ=environ) else int(environ.get("LEDGER_PORT", "8765"))
    return environ.get("LEDGER_HOST", "127.0.0.1"), port


def public_url(environ=os.environ) -> urllib.parse.SplitResult | None:
    url = environ.get("SWARM_PUBLIC_URL")
    if not url:
        return None
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"SWARM_PUBLIC_URL must be an http or https URL with a host, not {url!r}")
    return parts


def listed_hosts(environ=os.environ) -> set[str]:
    return {name.strip() for name in environ.get("SWARM_ALLOWED_HOSTS", "").split(",")} - {""}


def allowed_hosts(environ=os.environ) -> set[str]:
    host, port = address(environ)
    public = public_url(environ)
    hosts = {f"{name}:{port}" for name in (host, *LOOPBACK)} | listed_hosts(environ)
    return hosts | ({public.netloc} if public else set())


def allowed_origins(environ=os.environ) -> set[str]:
    host, port = address(environ)
    public = public_url(environ)
    origins = {f"http://{name}:{port}" for name in (host, *LOOPBACK)}
    origins |= {f"{scheme}://{name}" for name in listed_hosts(environ) for scheme in ("http", "https")}
    return origins | ({f"{public.scheme}://{public.netloc}"} if public else set())


def page_url(slug):
    return f"{base()}/{slug}"


def folder(environ=os.environ) -> Path:
    return Path(environ.get("LEDGER_DIR", Path.home() / "development-ledger")).expanduser()


def serving(timeout: float = 1, url: str | None = None) -> str | None:
    try:
        with urllib.request.urlopen(f"{url or base()}/healthz", timeout=timeout) as resp:
            body = json.loads(resp.read())
    except http.client.RemoteDisconnected:
        return None
    except (urllib.error.HTTPError, http.client.HTTPException, ValueError):
        return ""
    except OSError:
        return None
    served = body.get("dir") if isinstance(body, dict) else None
    return served if isinstance(served, str) else ""


def page_line(slug):
    line = f"Ledger page: {page_url(slug)} (open it to follow and steer the work)"
    served = serving()
    if served is None:
        return f"{line}. The ledger server is not answering: start it with {START}"
    if not served or Path(served).resolve() != folder().resolve():
        return (
            f"No ledger page link: {base()} serves {served or 'no ledger folder'}, not {folder()}. "
            f"Set a spare LEDGER_PORT and start the ledger server with {START}"
        )
    return line
