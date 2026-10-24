import json
import subprocess
from collections.abc import Callable

from scripts.swarm import ledger_events, mutation_wait, staged_compatibility
from scripts.swarm.store import SwarmError

FRESHNESS = """query($url: URI!) { resource(url: $url) { ... on PullRequest {
    repository { nameWithOwner }
    baseRefName baseRef { branchProtectionRule { requiredStatusCheckContexts requiredStatusChecks { context app { databaseId } } } }
} } }"""
FIELDS = 'id state headRefOid baseRefName mergeQueueEntry { id position state } isDraft mergeable reviewDecision statusCheckRollup { state } repository { mergeQueue(branch: "dev") { id } } body'
STATE = "query($url: URI!) { resource(url: $url) { ... on PullRequest { " + FIELDS + " } } }"
QUEUE = """mutation($id: ID!, $head: GitObjectID!) {
    enqueuePullRequest(input: {pullRequestId: $id, expectedHeadOid: $head}) { mergeQueueEntry { id } }
}"""

DEQUEUE = """mutation($id: ID!) {
    dequeuePullRequest(input: {id: $id}) { mergeQueueEntry { id } }
}"""


MERGE = """mutation($id: ID!, $head: GitObjectID!) {
  mergePullRequest(input: {pullRequestId: $id, expectedHeadOid: $head, mergeMethod: SQUASH}) {
    pullRequest { state }
  }
}"""


def graphql(query: str, variables: dict, run=subprocess.run) -> dict:
    command = ["gh", "api", "graphql", "-f", f"query={query}"]
    for name, value in variables.items():
        command += ["-f", f"{name}={value}"]
    try:
        done = run(command, capture_output=True, text=True, timeout=20)
        if done.returncode:
            raise SwarmError(done.stderr.strip() or "GitHub API failed")
        response = json.loads(done.stdout)
        if response.get("errors"):
            raise SwarmError("; ".join(error["message"] for error in response["errors"]))
        data = response["data"]
        if not isinstance(data, dict):
            raise SwarmError("GitHub API returned no data")
        return data
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as exc:
        raise SwarmError(f"GitHub API: {exc}") from exc


def rest(path: str, run) -> dict | list | str:
    if mutation_wait.backing_off():
        raise SwarmError(f"GitHub API rate limit backoff holds {path}")
    command = ["gh", "api", path]
    try:
        done = run(command, capture_output=True, text=True, timeout=20)
        if done.returncode:
            if mutation_wait.rate_limited(done.stderr):
                mutation_wait.hold(path)
            raise SwarmError(done.stderr.strip() or "GitHub API failed")
        return done.stdout if path.endswith("/logs") else json.loads(done.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise SwarmError(f"GitHub API: {exc}") from exc


def required_view(url: str, run) -> "ledger_events.PullRequest | None":
    def read(command, **kwargs):
        try:
            done = run(command, **kwargs)
        except (OSError, subprocess.SubprocessError) as exc:
            raise SwarmError(f"GitHub API: {exc}") from exc
        if done.returncode:
            raise SwarmError(done.stderr.strip() or "GitHub API failed")
        return done

    resource = graphql(FRESHNESS, {"url": url}, run)["resource"]
    repo = resource["repository"]["nameWithOwner"]
    rules = rest(f"repos/{repo}/rules/branches/{resource['baseRefName']}", run)
    protection = (resource.get("baseRef") or {}).get("branchProtectionRule") or {}
    expected = dict.fromkeys(protection.get("requiredStatusCheckContexts") or [])
    expected.update(
        (check["context"], (check.get("app") or {}).get("databaseId"))
        for check in protection.get("requiredStatusChecks") or []
    )
    expected.update(
        (check["context"], check.get("integration_id"))
        for rule in rules
        if rule["type"] == "required_status_checks"
        for check in rule["parameters"]["required_status_checks"]
    )
    return ledger_events.view(url, read, required_checks=expected)


def require_green(url: str, head: str, run) -> None:
    pull = required_view(url, run)
    if pull is None or pull.head != head or not pull.resolved or pull.red:
        failed = f"; failed: {', '.join(pull.failed)}" if pull and pull.failed else ""
        raise SwarmError(f"required checks on the current pull request head must pass before queueing{failed}")


def journaled(journal, action: str, url: str, head: str, call: Callable[[], dict]) -> dict:
    if journal is None:
        return call()
    before = journal.groups()
    errors = []
    try:
        return call()
    except SwarmError as exc:
        errors.append(str(exc))
        raise
    finally:
        journal.write(action, url, head, before, journal.groups(), errors)


def operate(action: str, url: str, run=subprocess.run, admit=None, journal=None) -> dict:
    raw = graphql(STATE, {"url": url}, run)["resource"]
    if not raw:
        raise SwarmError("GitHub URL does not identify a pull request")
    if action != "state" and raw["baseRefName"] != "dev":
        raise SwarmError("swarm merge queue operations require a pull request into dev")
    if action == "queue" and raw["mergeQueueEntry"] is None:
        staged_compatibility.require(url, raw["headRefOid"], raw.get("body") or "", run)
        if admit is not None:
            admit(raw["headRefOid"])
        if raw["repository"]["mergeQueue"] is None:
            if raw["isDraft"] or raw["reviewDecision"] not in (None, "APPROVED"):
                raise SwarmError("squash merge requires a ready pull request with green checks and no pending review")
            require_green(url, raw["headRefOid"], run)
            graphql(MERGE, {"id": raw["id"], "head": raw["headRefOid"]}, run)
        else:
            require_green(url, raw["headRefOid"], run)
            variables = {"id": raw["id"], "head": raw["headRefOid"]}
            journaled(journal, "queue", url, raw["headRefOid"], lambda: graphql(QUEUE, variables, run))
        raw = graphql(STATE, {"url": url}, run)["resource"]
    if action == "dequeue":
        journaled(journal, "dequeue", url, raw["headRefOid"], lambda: graphql(DEQUEUE, {"id": raw["id"]}, run))
        raw = graphql(STATE, {"url": url}, run)["resource"]
    return {
        "url": url,
        "state": raw["state"],
        "head": raw["headRefOid"],
        "queued": raw["mergeQueueEntry"] is not None,
        "entry": raw["mergeQueueEntry"],
    }


def waiting_around(url: str, target: str, run) -> tuple[list[dict], list[dict]]:
    query = """query($url: URI!, $after: String) { resource(url: $url) { ... on PullRequest {
      repository { mergeQueue(branch: "dev") { entries(first: 100, after: $after) {
        nodes { state pullRequest { id url headRefOid } }
        pageInfo { hasNextPage endCursor }
      } } }
    } } }"""
    variables = {"url": url}
    ahead, behind = [], []
    waiting = ahead
    while True:
        page = graphql(query, variables, run)["resource"]["repository"]["mergeQueue"]["entries"]
        for entry in page["nodes"]:
            if entry["pullRequest"]["id"] == target:
                waiting = behind
            elif entry["state"] == "QUEUED":
                waiting.append(entry["pullRequest"])
        if not page["pageInfo"]["hasNextPage"]:
            return ahead, behind
        variables["after"] = page["pageInfo"]["endCursor"]


class Untracked:
    def add(self, pull: dict) -> None:
        pass

    def discard(self, url: str) -> None:
        pass


def prioritize(url: str, run=subprocess.run, journal=None, ahead=(), displaced=None) -> dict:
    displaced = displaced or Untracked()
    raw = graphql(STATE, {"url": url}, run)["resource"]
    if not raw or raw["baseRefName"] != "dev" or raw["state"] != "OPEN":
        raise SwarmError("queue priority requires an open pull request into dev")
    if raw.get("isDraft"):
        raise SwarmError("queue priority requires a ready pull request")
    if raw["repository"]["mergeQueue"] is None:
        raise SwarmError("queue priority requires a dev merge queue")
    staged_compatibility.require(url, raw["headRefOid"], raw.get("body") or "", run)
    require_green(url, raw["headRefOid"], run)
    displacing, behind = waiting_around(url, raw["id"], run)
    waiting = [pull for pull in displacing if pull["url"] not in ahead] + behind
    before = journal.groups() if journal is not None else None
    errors = []
    steps = []
    try:
        removed = []
        try:
            for pull in reversed(waiting):
                current = graphql(STATE, {"url": pull["url"]}, run)["resource"]
                if current["mergeQueueEntry"] is not None and current["mergeQueueEntry"]["state"] == "QUEUED":
                    removed.append(pull)
                    steps.append(["dequeue", pull["url"]])
                    displaced.add(pull)
                    graphql(DEQUEUE, {"id": pull["id"]}, run)
            current = graphql(STATE, {"url": url}, run)["resource"]
            if current["mergeQueueEntry"] is None:
                steps.append(["queue", url])
                graphql(QUEUE, {"id": raw["id"], "head": raw["headRefOid"]}, run)
        except SwarmError as exc:
            errors.append(str(exc))
        restored = []
        for pull in reversed(removed):
            try:
                current = graphql(STATE, {"url": pull["url"]}, run)["resource"]
                if current["mergeQueueEntry"] is None:
                    staged_compatibility.require(pull["url"], pull["headRefOid"], current.get("body") or "", run)
                    steps.append(["restore", pull["url"]])
                    graphql(QUEUE, {"id": pull["id"], "head": pull["headRefOid"]}, run)
                    restored.append(pull["url"])
                displaced.discard(pull["url"])
            except SwarmError as exc:
                errors.append(f"could not restore {pull['url']}: {exc}")
        return {"url": url, "displaced": restored, "errors": errors}
    finally:
        if journal is not None:
            journal.write("priority", url, raw["headRefOid"], before, journal.groups(), errors, steps=steps)
