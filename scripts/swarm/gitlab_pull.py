"""A GitLab merge request read in the shape the swarm's GitHub pull request readers return."""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING

from scripts.forge.gitlab import FetchError, MergeRequestState, Transport, merge_request_state, urlopen_transport
from scripts.forge.remote import TOKEN_ENV

if TYPE_CHECKING:
    from scripts.swarm.ledger_events import PullRequest

TRANSPORT: Transport = urlopen_transport
READ_ERRORS = (FetchError, OSError, ValueError, KeyError, TypeError)


def _state(url: str, env: Mapping[str, str], send: Transport | None) -> MergeRequestState | None:
    """Return the merge request state, or None when there is no token or GitLab does not answer it."""
    try:
        return merge_request_state(url, env.get(TOKEN_ENV), send or TRANSPORT)
    except READ_ERRORS:
        return None


def pull(url: str, env: Mapping[str, str] = os.environ, send: Transport | None = None) -> PullRequest | None:
    """Return the GitLab merge request at ``url`` as a pull request, or None when it cannot be read."""
    from scripts.swarm.ledger_events import PullRequest

    read = _state(url, env, send)
    if read is None:
        return None
    merged_at = int(datetime.fromisoformat(read.merged_at).timestamp() * 1000) if read.merged_at else None
    return PullRequest(
        read.state.upper(), merged_at, None, False, head=read.head, draft=read.draft, body=read.description
    )


def head(url: str, env: Mapping[str, str] = os.environ, send: Transport | None = None) -> str | None:
    """Return the head commit of the GitLab merge request at ``url``, or None when it cannot be read."""
    read = _state(url, env, send)
    return read.head if read else None
