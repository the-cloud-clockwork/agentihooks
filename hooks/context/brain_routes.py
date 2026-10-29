"""The one brain a session talks to, chosen from operator owned configuration and never from whatever BRAIN_URL holds.

The routes file names each authorized brain, the variable holding its bearer credential, the operator's allowlist of
origins and the personal default. A managed swarm session always takes its swarm binding. Every other session takes
its explicit route, else the default. A BRAIN_URL outside the allowlist, such as one a repository settings file
exported, refuses the brain for that session instead of receiving the credential. The routes file must be readable by
its owner alone, which a repository checkout never produces under a normal umask. Without a routes file and outside a
swarm the legacy BRAIN_URL path is left as it was.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping, MutableMapping
from pathlib import Path
from urllib.parse import urlsplit

from scripts.swarm import brain_binding

VARIABLE = "AGENTIHOOKS_BRAIN_ROUTE"
RECEIPT = "AGENTIHOOKS_BRAIN_RECEIPT"
FAILURES = "brain_route_resolution_failures"
SWARM = "swarm"
ROUTED, REFUSED = "routed", "refused"
FRESH, SAME, INVALIDATED, STALE = "fresh", "same", "invalidated", "stale"
LOOPBACK = ("localhost", "127.0.0.1", "::1")
SESSION_KEYS = ("BRAIN_URL", "BRAIN_HTTP_TOKEN", "KB_ROUTER_TOKEN")
UNAUTHORIZED_URL = "the session BRAIN_URL is not an authorized brain origin"
_REFERENCE = re.compile(r"[A-Z][A-Z0-9_]*")
_SESSION = re.compile(r"[A-Za-z0-9_.-]+")


class Refused(ValueError):
    pass


def home(environ: Mapping[str, str]) -> Path:
    return Path(environ.get("AGENTIHOOKS_HOME") or Path(environ.get("HOME") or Path.home()) / ".agentihooks")


def path(environ: Mapping[str, str]) -> Path:
    return home(environ) / "brain-routes.json"


def store(environ: Mapping[str, str]) -> Path:
    return home(environ) / "brain-routes"


def _routes(found: Path) -> dict | None:
    try:
        info = found.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise Refused("the brain routes file is not an operator owned file")
    try:
        doc = json.loads(found.read_bytes())
    except (OSError, ValueError):
        raise Refused("the brain routes file is not readable JSON") from None
    if not isinstance(doc, dict) or not isinstance(doc.get("routes"), dict):
        raise Refused("the brain routes file names no routes")
    return doc


def _allowed(url: object, allow: object) -> str:
    if not isinstance(url, str) or not isinstance(allow, list):
        raise Refused("the route names no allowlisted endpoint")
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    plain = host and not (parts.username or parts.password or parts.query or parts.fragment)
    secure = parts.scheme == "https" or (parts.scheme == "http" and host in LOOPBACK)
    if not (plain and secure and brain_binding.origin(url) in {brain_binding.origin(o) for o in allow}):
        raise Refused("the route endpoint is not an allowlisted brain origin")
    return url.removesuffix("/")


def _revision(route: Mapping[str, object]) -> str:
    return hashlib.sha256(json.dumps(route, sort_keys=True).encode()).hexdigest()


def _swarm(binding: dict, current: str | None) -> dict:
    if binding.get("status") != brain_binding.BOUND:
        raise Refused("the swarm brain binding refused this session")
    if current and brain_binding.origin(current) != brain_binding.origin(binding["api"]):
        raise Refused(UNAUTHORIZED_URL)
    route = {"route": SWARM, "api": binding["api"], "token_env": binding["api_env"]}
    return {**route, "generation": 0, "revision": _revision({**route, "instance": binding.get("instance", "")})}


def resolve(environ: Mapping[str, str]) -> dict | None:
    """The route this session must use; None on the legacy path; Refused names why there is no brain."""
    binding = brain_binding.current(environ)
    if binding is not None:
        return _swarm(binding, environ.get("BRAIN_URL"))
    doc = _routes(path(environ))
    if doc is None:
        return None
    name = environ.get(VARIABLE) or doc.get("default")
    entry = doc["routes"].get(name) if isinstance(name, str) else None
    if not isinstance(entry, dict):
        raise Refused(f"no authorized brain route named {name}" if environ.get(VARIABLE) else "no default brain route")
    api = _allowed(entry.get("api"), doc.get("allow"))
    reference = entry.get("token_env")
    if not isinstance(reference, str) or not _REFERENCE.fullmatch(reference) or reference in SESSION_KEYS:
        raise Refused(f"route {name} names no dedicated credential variable")
    if not environ.get(reference):
        raise Refused(f"credential {reference} is unset")
    allow = sorted(brain_binding.origin(o) for o in doc["allow"])
    route = {"route": name, "api": api, "token_env": reference}
    generation = doc.get("revision", 0)
    if not isinstance(generation, int) or isinstance(generation, bool):
        raise Refused("the brain routes revision is not an integer")
    current = environ.get("BRAIN_URL")
    if current and brain_binding.origin(current) not in allow:
        raise Refused(UNAUTHORIZED_URL)
    return {**route, "generation": generation, "revision": _revision({**route, "allow": allow})}


def _count(environ: Mapping[str, str], reason: str) -> None:
    folder = store(environ)
    route = SWARM if environ.get(brain_binding.VARIABLE) else environ.get(VARIABLE) or "default"
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / f"{FAILURES}.jsonl").open("a", encoding="utf-8") as log:
        log.write(json.dumps({"route": route, "reason": reason}) + "\n")


def failures(environ: Mapping[str, str]) -> int:
    try:
        return len((store(environ) / f"{FAILURES}.jsonl").read_text(encoding="utf-8").splitlines())
    except OSError:
        return 0


def apply(environ: MutableMapping[str, str]) -> dict | None:
    """Point the session's brain variables at its route and record the receipt; None leaves the legacy path."""
    try:
        route = resolve(environ)
    except Refused as exc:
        for key in SESSION_KEYS:
            environ.pop(key, None)
        environ["BRAIN_ENABLED"] = environ["BRAIN_WRITER_ENABLED"] = "false"
        receipt = {"status": REFUSED, "reason": str(exc)}
        environ[RECEIPT] = json.dumps(receipt)
        _count(environ, str(exc))
        return receipt
    if route is None:
        environ.pop(RECEIPT, None)
        return None
    if route["route"] != SWARM:
        environ.pop("KB_ROUTER_TOKEN", None)
        environ["BRAIN_URL"] = route["api"]
        environ["BRAIN_HTTP_TOKEN"] = environ[route["token_env"]]
    receipt = {
        "status": ROUTED,
        "route": route["route"],
        "origin": brain_binding.origin(route["api"]),
        "generation": route["generation"],
        "revision": route["revision"],
    }
    environ[RECEIPT] = json.dumps(receipt)
    return receipt


def remember(environ: Mapping[str, str], session: str) -> str | None:
    """Persist this session's receipt and say whether it is fresh, unchanged, invalidated by a route change or stale.

    A routed receipt from an older routes revision than the one stored is stale and leaves the newer receipt; a refusal
    always replaces it.
    """
    raw = environ.get(RECEIPT)
    if not raw or not _SESSION.fullmatch(session):
        return None
    receipt = json.loads(raw)
    found = store(environ) / "sessions" / f"{session}.json"
    try:
        previous = json.loads(found.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = None
    if previous == receipt:
        return SAME
    if previous and receipt["status"] == ROUTED and receipt["generation"] < previous.get("generation", 0):
        return STALE
    found.parent.mkdir(parents=True, exist_ok=True)
    found.write_text(json.dumps(receipt, sort_keys=True), encoding="utf-8")
    return FRESH if previous is None else INVALIDATED
