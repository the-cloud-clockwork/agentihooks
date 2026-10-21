"""The ledger page link handed to the operator, from the ledger server's configured host and port."""

import http.client
import ipaddress
import json
import os
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from scripts.swarm_ledger import hosted_probe, ledger_route

START = "agentihooks ledger serve --ensure"
LOOPBACK = ("127.0.0.1", "localhost")
USER_AGENT = "agentihooks-swarm"
CREDENTIAL_HEADERS = (
    "X-Controller-Credential",
    "X-Hive-Credential",
    "X-Ledger-Token",
    "X-Proof-Credential",
    "X-Ledger-Tick",
    "X-Ledger-Migration",
)
TLS_CERT_ENV, TLS_KEY_ENV, CA_ENV, CA_FILE_ENV = "LEDGER_TLS_CERT", "LEDGER_TLS_KEY", "LEDGER_CA_PEM", "LEDGER_CA_FILE"


class Cleartext(SystemExit):
    pass


def base(environ=os.environ, slug="") -> str:
    if selected := ledger_route.url(slug) or hosted_probe.hosted(environ):
        return selected
    if remote(environ, slug):
        url = environ.get("SWARM_URL") or environ.get("LEDGER_URL")
        if not url:
            raise SystemExit("a remote ledger client needs SWARM_URL, the address of the hosted ledger server")
        return url.rstrip("/")
    host, port = address(environ)
    return f"{scheme(environ)}://{host}:{port}"


def scheme(environ=os.environ) -> str:
    return "https" if tls(environ) else "http"


def tls(environ=os.environ) -> tuple[str, str] | None:
    cert, key = environ.get(TLS_CERT_ENV, "").strip(), environ.get(TLS_KEY_ENV, "").strip()
    if bool(cert) != bool(key):
        raise SystemExit(f"the ledger serves TLS only with both {TLS_CERT_ENV} and {TLS_KEY_ENV}")
    return (cert, key) if cert else None


def server_context(environ=os.environ) -> ssl.SSLContext | None:
    found = tls(environ)
    if found is None:
        return None
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(*found)
    return context


def loopback(host: str | None) -> bool:
    try:
        found = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except (OSError, TypeError, UnicodeError):
        return False
    return bool(found) and all(ipaddress.ip_address(address).is_loopback for address in found)


def guard(url: str, headers: dict) -> None:
    parts = urllib.parse.urlsplit(url)
    sent = {name.lower() for name, value in headers.items() if value}
    if parts.scheme == "https" or not sent & {name.lower() for name in CREDENTIAL_HEADERS}:
        return
    if not loopback(parts.hostname):
        raise Cleartext(
            f"a ledger credential off loopback travels only over https, not to {parts.scheme}://{parts.netloc}: "
            f"serve the ledger with {TLS_CERT_ENV} and {TLS_KEY_ENV} and address it as https"
        )


def transport(environ=os.environ) -> dict:
    pem = environ.get(CA_ENV, "").strip()
    if pem:
        return {"context": ssl.create_default_context(cadata=pem)}
    found = ca_file(environ)
    if not found.is_file():
        if environ.get(CA_FILE_ENV):
            raise SystemExit(f"{CA_FILE_ENV} names no file: {found}")
        return {}
    context = ssl.create_default_context()
    try:
        context.load_verify_locations(cafile=str(found))
    except ssl.SSLError as error:
        raise SystemExit(f"the ledger CA file {found} holds no valid certificate: {error.reason}") from error
    return {"context": context}


def ca_file(environ=os.environ) -> Path:
    if environ.get(CA_FILE_ENV):
        return Path(environ[CA_FILE_ENV]).expanduser()
    return Path(environ.get("AGENTIHOOKS_HOME") or Path.home() / ".agentihooks") / "ledger-ca.pem"


def urlopen(request: urllib.request.Request, timeout: float, environ=os.environ):
    for name, value in access_headers(environ).items():
        request.add_header(name, value)
    guard(request.full_url, dict(request.header_items()))
    return urllib.request.urlopen(request, timeout=timeout, **transport(environ))


def remote(environ=os.environ, slug="") -> bool:
    return bool(
        ledger_route.url(slug)
        or hosted_probe.hosted(environ)
        or (environ.get("SWARM_URL") if not slug or environ.get("AGENTIHOOKS_SWARM", slug) == slug else "")
    ) or not local_mode(environ)


def local_mode(environ=os.environ) -> bool:
    mode = environ.get("AGENTIHOOKS_DEPLOYMENT")
    return not environ.get("LEDGER_URL") if mode is None else mode == "local"


def access_headers(environ=os.environ) -> dict:
    client = environ.get("SWARM_ACCESS_CLIENT_ID") or environ.get("CF_ACCESS_CLIENT_ID")
    secret = environ.get("SWARM_ACCESS_CLIENT_SECRET") or environ.get("CF_ACCESS_CLIENT_SECRET")
    if not (client and secret):
        return {}
    return {"CF-Access-Client-Id": client, "CF-Access-Client-Secret": secret, "User-Agent": USER_AGENT}


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
    origins = {f"{scheme(environ)}://{name}:{port}" for name in (host, *LOOPBACK)}
    origins |= {f"{scheme}://{name}" for name in listed_hosts(environ) for scheme in ("http", "https")}
    return origins | ({f"{public.scheme}://{public.netloc}"} if public else set())


def page_url(slug):
    return f"{base(slug=slug)}/{slug}"


def folder(environ=os.environ) -> Path:
    return Path(environ.get("LEDGER_DIR", Path.home() / "development-ledger")).expanduser()


def serving(timeout: float = 1, url: str | None = None) -> str | None:
    try:
        with urlopen(urllib.request.Request(f"{url or base()}/healthz"), timeout) as resp:
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
