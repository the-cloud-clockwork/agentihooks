"""The signed lab head notice an agent's push sends to the webhook receiver's `/lab/head` route."""

import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

URL_ENV = "AGENTIHOOKS_CI_LAB_NOTICE_URL"
ACTOR_ENV = "AGENTIHOOKS_CI_LAB_NOTICE_ACTOR"

Opener = Callable[..., Any]


def send(
    environ: Mapping[str, str], repo: str, head: str, base: str, opener: Opener = urllib.request.urlopen
) -> int | None:
    """Post the signed notice of a pushed head to the receiver the environment names and return its status."""
    raise NotImplementedError
