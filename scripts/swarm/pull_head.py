"""Confirm a pull request with GitHub before swarm pr records it on a task."""

import argparse
import os
from collections.abc import Callable, Mapping
from typing import Any, NamedTuple

from scripts.forge.gitlab import Transport, urlopen_transport
from scripts.forge.remote import ChangeLink
from scripts.swarm import stack
from scripts.swarm.store import SwarmError
from scripts.tick.deadline import Deadline
from scripts.tick.ports.contracts import SourceControl
from scripts.tick.ports.source_control import ChangeRef

PULL_SECONDS = 20


class PullHead(NamedTuple):
    """The head branch, its repository url, the number, the head commit and the body of a pull request GitHub
    confirmed."""

    branch: str
    repo: str
    number: int = 0
    head: str = ""
    body: str = ""


def gitlab_change(
    link: ChangeLink, send: Transport = urlopen_transport, env: Mapping[str, str] = os.environ
) -> tuple[str, PullHead]:
    """Read the GitLab merge request ``link`` names into its state and pull head."""
    raise ValueError(f"{link.web} is not read yet")


def pull_branch(
    url: str,
    ports: Callable[[str], tuple[SourceControl, ChangeRef]] = stack.source_control,
    forge: Callable[[ChangeLink], tuple[str, PullHead]] = gitlab_change,
) -> PullHead:
    """Read an open pull request's head branch, repository url, number, head and body, refusing one GitHub does not
    confirm open."""
    try:
        provider, reference = ports(url)
        change = provider.change(reference, deadline=Deadline.after(PULL_SECONDS))
    except stack.PORT_ERRORS as exc:
        raise SwarmError(f"GitHub does not confirm pull request {url}: {exc}") from exc
    if change.state != "open":
        raise SwarmError(f"pull request {url} is {change.state}, not open")
    host = url.split("/pull/")[0].rsplit("/", 2)[0]
    repo = f"{host}/{change.head_repository}" if change.head_repository else ""
    return PullHead(change.head_branch, repo, change.reference.number, change.head, change.body)


def refuse_other_branch(row: dict[str, Any], args: argparse.Namespace, pull: PullHead) -> None:
    """Refuse an undeclared pull request of the task's repository whose head is not the task's recorded branch."""
    recorded = row.get("branch") or ""
    declared = args.stage is not None or args.prerequisite is not None or bool(row.get("split"))
    if declared or not pull.branch or not recorded or pull.branch == recorded:
        return
    if pull.repo and repo_key(row.get("branch_repo") or "") not in ("", repo_key(pull.repo)):
        return
    raise SwarmError(f"pull request {args.url} is on branch {pull.branch}, not this task's recorded branch {recorded}")


def repo_key(url: str) -> str:
    """Return ``url`` lowercased without a trailing git suffix, so two spellings of one repository compare equal."""
    return url.lower().removesuffix(".git")
