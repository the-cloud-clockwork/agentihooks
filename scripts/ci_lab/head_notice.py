"""The signed lab head notice an agent's push sends to the webhook receiver's `/lab/head` route.

`send` posts one notice naming a pushed head when the environment names the receiver, the lab notice secret and the
actor, and sends nothing without all three. A notice the receiver refuses or that cannot reach it is reported on
standard error and never raised, so a failed send never fails the push before it.
"""

import hashlib
import hmac
import json
import sys
import urllib.request
from collections.abc import Callable, Mapping
from http.client import HTTPException
from typing import Any

from scripts.ci_lab.webhooks import NOTICE_SECRET_ENV

URL_ENV = "AGENTIHOOKS_CI_LAB_NOTICE_URL"
ACTOR_ENV = "AGENTIHOOKS_CI_LAB_NOTICE_ACTOR"
SIGNATURE = "X-Hub-Signature-256"
TIMEOUT_S = 5

Opener = Callable[..., Any]


def signature(secret: bytes, body: bytes) -> int:
    """Return the X-Hub-Signature-256 value of body: sha256= and the hex HMAC-SHA256 of its exact bytes."""
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


def notice(url: str, secret: bytes, fields: Mapping[str, str]) -> urllib.request.Request:
    """Return the POST to url of one head notice whose JSON body carries fields, signed with secret."""
    body = json.dumps(dict(fields)).encode()
    return urllib.request.Request(url, data=body, headers={SIGNATURE: signature(secret, body)})


def send(
    environ: Mapping[str, str], repo: str, head: str, base: str, opener: Opener = urllib.request.urlopen
) -> int | None:
    """Post the signed notice of a pushed head and return the receiver's status, 0 when the send failed, or None when
    the environment does not name a receiver, a secret and an actor."""
    url, secret, actor = environ.get(URL_ENV), environ.get(NOTICE_SECRET_ENV), environ.get(ACTOR_ENV)
    if not (url and secret and actor):
        return None
    request = notice(url, secret.encode(), {"repo": repo, "head": head, "base": base, "actor": actor})
    try:
        with opener(request, timeout=TIMEOUT_S) as answer:
            return answer.status
    except (OSError, ValueError, HTTPException) as exc:
        print(f"lab head notice for {head} was not accepted: {exc!r}; the push stands", file=sys.stderr)
        return 0
