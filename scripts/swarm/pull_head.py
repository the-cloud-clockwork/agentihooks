"""Confirm a pull request with GitHub before swarm pr records it on a task."""

import argparse
from collections.abc import Callable
from typing import Any, NamedTuple

from scripts.swarm import stack
from scripts.swarm.store import SwarmError
from scripts.tick.deadline import Deadline
from scripts.tick.ports.contracts import SourceControl
from scripts.tick.ports.source_control import ChangeRef

PULL_SECONDS = 20


class PullHead(NamedTuple):
    """The head branch, its repository url, the number and the head commit of a pull request GitHub confirmed."""

    branch: str
    repo: str
    number: int = 0
    head: str = ""


def pull_branch(url: str, ports: Callable[[str], tuple[SourceControl, ChangeRef]] = stack.source_control) -> PullHead:
    """Read an open pull request's head branch, repository url, number and head, refusing one GitHub does not
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
    return PullHead(change.head_branch, repo, change.reference.number, change.head)


def refuse_other_branch(row: dict[str, Any], args: argparse.Namespace, pull: PullHead, keeps_main: bool) -> None:
    """Refuse an undeclared pull request of the task's repository whose head is not the task's recorded branch."""
    recorded = row.get("branch") or ""
    declared = keeps_main or args.stage is not None or args.prerequisite is not None or bool(row.get("split"))
    if declared or not pull.branch or not recorded or pull.branch == recorded:
        return
    if repo_key(row.get("branch_repo") or "") not in ("", repo_key(pull.repo)):
        return
    raise SwarmError(f"pull request {args.url} is on branch {pull.branch}, not this task's recorded branch {recorded}")


def repo_key(url: str) -> str:
    """Return ``url`` lowercased without a trailing git suffix, so two spellings of one repository compare equal."""
    return url.lower().removesuffix(".git")
