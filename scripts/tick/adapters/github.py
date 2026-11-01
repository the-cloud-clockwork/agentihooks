"""GitHub implementation of the source control port over audited GitHub reads and git."""

import json
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.swarm import github_reads
from scripts.tick.deadline import Deadline
from scripts.tick.ports.contracts import Unsupported
from scripts.tick.ports.source_control import (
    Capabilities,
    ChangeRef,
    ChangeState,
    CheckState,
    Comment,
    Merged,
    QueueEntry,
    QueueHealth,
    RateLimit,
    RepositoryRun,
    Run,
    Tag,
)

__all__ = ["CALLER", "GitHubSourceControl", "command"]

CALLER = "tick.source_control"

_PULL = """query($owner: String!, $name: String!, $number: Int!, $base: String!) {
  repository(owner: $owner, name: $name) {
    mergeQueue(branch: $base) { id }
    pullRequest(number: $number) { id headRefOid }
  }
}"""

_ENQUEUE = """mutation($id: ID!, $head: GitObjectID!) {
  enqueuePullRequest(input: {pullRequestId: $id, expectedHeadOid: $head}) { mergeQueueEntry { position } }
}"""

_QUEUE = """query($owner: String!, $name: String!, $base: String!) {
  repository(owner: $owner, name: $name) {
    mergeQueue(branch: $base) { entries(first: 100) { totalCount nodes { state } } }
  }
}"""

Git = Callable[..., subprocess.CompletedProcess[str]]

_PAGE = 100
_MAX_PAGES = 10


def command(args: list[str]) -> list[str]:
    """Return the gh command line for the arguments; the GitHub adapter is the only module that names gh."""
    return ["gh", *args]


def _live(deadline: Deadline) -> float:
    """Return the seconds left on the deadline, refusing an expired one before any external call."""
    if deadline.expired:
        raise TimeoutError("tool deadline expired")
    return deadline.remaining


@dataclass(frozen=True)
class GitHubSourceControl:
    """Source control port answered by the GitHub REST and GraphQL APIs and the git remote."""

    repository: str
    root: Path
    base: str
    git: Git = subprocess.run

    def _call(self, args: list[str], deadline: Deadline) -> str:
        """Run one audited gh api call inside the deadline and return its output."""
        timeout = _live(deadline)
        result = github_reads.rest(
            CALLER, ["api", *args], cwd=self.root, timeout=timeout, capture_output=True, text=True, check=True
        )
        return result.stdout

    def _api(self, args: list[str], deadline: Deadline) -> Any:
        """Run one audited gh api call inside the deadline and decode its JSON body."""
        return json.loads(self._call(args, deadline))

    def _pages(self, path: str, key: str, deadline: Deadline) -> list[Any]:
        """Read every page of a listing until a short page, at most the page limit."""
        rows: list[Any] = []
        for page in range(1, _MAX_PAGES + 1):
            batch = self._api([f"{path}per_page={_PAGE}&page={page}"], deadline)[key]
            rows.extend(batch)
            if len(batch) < _PAGE:
                break
        return rows

    def _graphql(self, query: str, variables: Mapping[str, str | int], deadline: Deadline) -> Any:
        """Run one GraphQL document with string and integer variables and return its data."""
        flags = [
            word
            for name, value in variables.items()
            for word in ("-F" if isinstance(value, int) else "-f", f"{name}={value}")
        ]
        return self._api(["graphql", "-f", f"query={query}", *flags], deadline)["data"]

    def _queue(self, deadline: Deadline) -> Any:
        """Read the merge queue of the base branch, None when the repository has none."""
        owner, name = self.repository.split("/")
        variables = {"owner": owner, "name": name, "base": self.base}
        return self._graphql(_QUEUE, variables, deadline)["repository"]["mergeQueue"]

    def change(self, reference: ChangeRef, *, deadline: Deadline) -> ChangeState:
        """Read the state of one change."""
        body = self._api([f"repos/{reference.repository}/pulls/{reference.number}"], deadline)
        return ChangeState(
            reference,
            body["state"],
            body["head"]["sha"],
            body["base"]["ref"],
            body["merged"],
            body["html_url"],
            body["head"]["ref"],
        )

    def checks(self, reference: ChangeRef, *, deadline: Deadline) -> tuple[CheckState, ...]:
        """Read the check runs on the change's current head."""
        head = self.change(reference, deadline=deadline).head
        rows = self._pages(f"repos/{reference.repository}/commits/{head}/check-runs?", "check_runs", deadline)
        return tuple(CheckState(row["name"], row["status"], row["conclusion"], row["html_url"]) for row in rows)

    def runs(self, reference: ChangeRef, *, deadline: Deadline) -> tuple[Run, ...]:
        """Read the workflow runs on the change's current head."""
        head = self.change(reference, deadline=deadline).head
        rows = self._pages(f"repos/{reference.repository}/actions/runs?head_sha={head}&", "workflow_runs", deadline)
        return tuple(
            Run(row["id"], row["name"], row["head_sha"], row["status"], row["conclusion"], row["html_url"])
            for row in rows
        )

    def enqueue(self, reference: ChangeRef, *, deadline: Deadline) -> QueueEntry | Unsupported:
        """Queue the change at its current head, or answer Unsupported without a merge queue."""
        owner, name = reference.repository.split("/")
        variables = {"owner": owner, "name": name, "number": reference.number, "base": self.base}
        repository = self._graphql(_PULL, variables, deadline)["repository"]
        if repository["mergeQueue"] is None:
            return Unsupported("enqueue")
        pull = repository["pullRequest"]
        data = self._graphql(_ENQUEUE, {"id": pull["id"], "head": pull["headRefOid"]}, deadline)
        return QueueEntry(reference, data["enqueuePullRequest"]["mergeQueueEntry"]["position"])

    def merge(self, reference: ChangeRef, *, deadline: Deadline) -> Merged:
        """Squash merge the change pinned to the head it was read at."""
        head = self.change(reference, deadline=deadline).head
        path = f"repos/{reference.repository}/pulls/{reference.number}/merge"
        body = self._api([path, "-X", "PUT", "-f", f"sha={head}", "-f", "merge_method=squash"], deadline)
        return Merged(reference, body["sha"])

    def queue_health(self, *, deadline: Deadline) -> QueueHealth | Unsupported:
        """Read the depth and entry states of the base branch merge queue."""
        queue = self._queue(deadline)
        if queue is None:
            return Unsupported("queue_health")
        entries = queue["entries"]
        return QueueHealth(entries["totalCount"], tuple(node["state"] for node in entries["nodes"]))

    def tags(self, *, deadline: Deadline) -> tuple[Tag, ...]:
        """List the remote tags with the commits they name."""
        timeout = _live(deadline)
        result = self.git(
            ["git", "ls-remote", "--tags", "origin"],
            cwd=self.root,
            timeout=timeout,
            capture_output=True,
            text=True,
            check=True,
        )
        rows = [line.partition("\t") for line in result.stdout.splitlines() if line]
        return tuple(Tag(ref.removeprefix("refs/tags/"), sha) for sha, _, ref in rows if not ref.endswith("^{}"))

    def comment(self, reference: ChangeRef, body: str, *, deadline: Deadline) -> Comment:
        """Post a comment on the change."""
        path = f"repos/{reference.repository}/issues/{reference.number}/comments"
        posted = self._api([path, "-f", f"body={body}"], deadline)
        return Comment(reference, posted["html_url"])

    def capabilities(self, *, deadline: Deadline) -> Capabilities:
        """Report which optional operations this repository supports."""
        return Capabilities(merge_queue=self._queue(deadline) is not None)

    def issue_state(self, reference: ChangeRef, *, deadline: Deadline) -> str:
        """Read whether the issue is open or closed."""
        return self._api([f"repos/{reference.repository}/issues/{reference.number}"], deadline)["state"]

    def close_issue(self, reference: ChangeRef, *, deadline: Deadline) -> str:
        """Close the issue and return the state it reports after the write."""
        path = f"repos/{reference.repository}/issues/{reference.number}"
        return self._api([path, "-X", "PATCH", "-f", "state=closed"], deadline)["state"]

    def rate_limit(self, *, deadline: Deadline) -> RateLimit:
        """Read the core REST quota of the login, a read GitHub does not charge."""
        core = self._api(["rate_limit"], deadline)["resources"]["core"]
        return RateLimit(core["used"], core["remaining"], core["reset"])

    def issues_enabled(self, *, deadline: Deadline) -> bool:
        """Read whether the repository takes issues."""
        return self._api([f"repos/{self.repository}"], deadline)["has_issues"]

    def latest_runs(self, *, deadline: Deadline) -> tuple[RepositoryRun, ...]:
        """Read the newest workflow runs of the repository, one page."""
        rows = self._api([f"repos/{self.repository}/actions/runs?per_page={_PAGE}"], deadline)["workflow_runs"]
        return tuple(RepositoryRun(row["id"], row["head_branch"], row["event"], row["status"]) for row in rows)

    def cancel_run(self, run_id: int, *, deadline: Deadline) -> None:
        """Ask GitHub to cancel one workflow run of the repository; its answer carries no body to read."""
        self._call([f"repos/{self.repository}/actions/runs/{run_id}/cancel", "-X", "POST"], deadline)
