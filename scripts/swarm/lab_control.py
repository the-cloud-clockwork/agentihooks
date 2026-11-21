"""The swarm's orders to its CI lab's control routes: hotfix priority for heads, a pause of pull request grading but
the excepted heads, and resume, each sent with the bearer token this host holds in CI_LAB_CONTROL_TOKEN."""

import json
from collections.abc import Iterable, Mapping
from typing import Any, NamedTuple, TypeGuard
from urllib.error import HTTPError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from scripts.ci_lab.contract import CONTROL_PORT, CONTROL_TOKEN

TOKEN = CONTROL_TOKEN
TIMEOUT_S = 3
HOTFIX = "hotfix"
PULL_REQUEST = "pull_request"
UNAUTHORIZED = 401
AUTHORIZATION = "Authorization"
JSON_BODY = {"Content-Type": "application/json"}


class LabError(Exception):
    """Why the lab could not be told."""


class Lab(NamedTuple):
    """The lab's control port address and the bearer token its control routes take."""

    url: str
    token: str


def lab(url: str, environ: Mapping[str, str]) -> Lab:
    """Return the swarm's lab control routes on the control port of the host at ``url``, with this host's token;
    refuse a swarm without a lab url or a host without a token."""
    if not url:
        raise LabError("the swarm has no lab url")
    token = environ.get(TOKEN) or ""
    if not token:
        raise LabError(f"{TOKEN} is not set on this host")
    parts = urlsplit(url)
    host = parts.netloc.rsplit(":", 1)[0] if parts.port is not None else parts.netloc
    return Lab(urlunsplit((parts.scheme, f"{host}:{CONTROL_PORT}", "", "", "")), token)


def _is_map(value: object) -> TypeGuard[dict[str, Any]]:
    return isinstance(value, dict)


def post(target: Lab, path: str, body: Mapping[str, object]) -> dict[str, Any]:
    """POST ``body`` to the control route ``path`` and return the lab's JSON reply; refuse a lab that cannot be
    reached, refuses the token or answers anything but a JSON map."""
    headers = {AUTHORIZATION: f"Bearer {target.token}", **JSON_BODY}
    request = Request(f"{target.url.rstrip('/')}{path}", json.dumps(body).encode(), headers)
    try:
        with urlopen(request, timeout=TIMEOUT_S) as reply:
            raw = reply.read()
    except HTTPError as error:
        answered = "refused the control token" if error.code == UNAUTHORIZED else f"answered {error.code} on {path}"
        raise LabError(f"the lab at {target.url} {answered}") from error
    except OSError as error:
        raise LabError(f"the lab at {target.url} is unreachable: {error}") from error
    refused = LabError(f"the lab at {target.url} answered {path} with no JSON map")
    try:
        found: object = json.loads(raw)
    except ValueError as error:
        raise refused from error
    if not _is_map(found):
        raise refused
    return found


def priority(target: Lab, heads: Iterable[str]) -> dict[str, Any]:
    """Mark the heads hotfix in the lab."""
    return post(target, "/priority", {"heads": sorted(set(heads)), "class": HOTFIX})


def pause(target: Lab, excepted: Iterable[str]) -> dict[str, Any]:
    """Hold the lab's pull request grading but the excepted heads."""
    return post(target, "/pause", {"class": PULL_REQUEST, "except": sorted(set(excepted))})


def resume(target: Lab) -> dict[str, Any]:
    """Lift every pause in the lab."""
    return post(target, "/resume", {})


def acknowledge(url: str, head: str, environ: Mapping[str, str]) -> dict[str, Any]:
    """Mark ``head`` hotfix in the lab at ``url`` and return the lab's reply, or ``{"error": why}`` when it cannot be
    told."""
    try:
        return priority(lab(url, environ), [head])
    except LabError as error:
        return {"error": str(error)}
