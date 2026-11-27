"""Confirm a GitHub pull request or a GitLab merge request before swarm pr records it on a task."""

import argparse
import json
import os
from collections.abc import Callable, Mapping
from typing import Any, NamedTuple

from scripts.forge.gitlab import GitLabForge, Transport, urlopen_transport
from scripts.forge.remote import CHANGE_LINK, TOKEN_ENV, ChangeLink, change_link
from scripts.swarm import stack
from scripts.swarm.store import SwarmError
from scripts.tick.deadline import Deadline
from scripts.tick.ports.contracts import SourceControl
from scripts.tick.ports.source_control import ChangeRef

PULL_SECONDS = 20
GITLAB_STATES = {"opened": "open"}


class PullHead(NamedTuple):
    """The head branch, its repository url, the number, the head commit and the body of a pull request its forge
    confirmed."""

    branch: str
    repo: str
    number: int = 0
    head: str = ""
    body: str = ""


def gitlab_change(
    link: ChangeLink, send: Transport = urlopen_transport, env: Mapping[str, str] = os.environ
) -> tuple[str, PullHead]:
    """Read the GitLab merge request ``link`` names into its state, opened reading as open, and its pull head, whose
    repository is the source project, refusing without a GitLab token."""
    token = env.get(TOKEN_ENV)
    if not token:
        raise ValueError(f"{TOKEN_ENV} is not set")
    forge = GitLabForge(link.host, link.path, token, send)
    raw = _gitlab_read(forge, f"{forge.api}/{link.number}")
    source = raw["source_project_id"]
    project = f"https://{link.host}/api/v4/projects/{source}"
    path = link.path if source == raw["target_project_id"] else _gitlab_read(forge, project)["path_with_namespace"]
    repo = f"https://{link.host}/{path}"
    head = PullHead(raw["source_branch"], repo, raw["iid"], raw["sha"], raw["description"] or "")
    state: str = raw["state"]
    return GITLAB_STATES.get(state, state), head


def gitlab_link(url: str) -> ChangeLink | None:
    """Return ``url`` parsed when it is a GitLab merge request link, else None."""
    link = change_link(url) if CHANGE_LINK.fullmatch(url) else None
    return link if link and link.kind == "gitlab" else None


def _gitlab_read(forge: GitLabForge, url: str) -> Any:
    return json.loads(forge.send("GET", url, {"PRIVATE-TOKEN": forge.token}, None))


def pull_branch(
    url: str,
    ports: Callable[[str], tuple[SourceControl, ChangeRef]] = stack.source_control,
    forge: Callable[[ChangeLink], tuple[str, PullHead]] = gitlab_change,
) -> PullHead:
    """Read an open pull request's head branch, repository url, number, head and body, refusing one its forge does
    not confirm open: a GitLab merge request link reads through ``forge``, any other link through ``ports``."""
    link = gitlab_link(url)
    try:
        state, pull = forge(link) if link else _github_pull(url, ports)
    except stack.PORT_ERRORS as exc:
        refusal = "GitLab does not confirm merge request" if link else "GitHub does not confirm pull request"
        raise SwarmError(f"{refusal} {url}: {exc}") from exc
    if state != "open":
        raise SwarmError(f"pull request {url} is {state}, not open")
    return pull


def _github_pull(url: str, ports: Callable[[str], tuple[SourceControl, ChangeRef]]) -> tuple[str, PullHead]:
    provider, reference = ports(url)
    change = provider.change(reference, deadline=Deadline.after(PULL_SECONDS))
    host = url.split("/pull/")[0].rsplit("/", 2)[0]
    repo = f"{host}/{change.head_repository}" if change.head_repository else ""
    return change.state, PullHead(change.head_branch, repo, change.reference.number, change.head, change.body)


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
