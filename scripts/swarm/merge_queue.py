import json
import subprocess

from scripts.swarm import ledger_events
from scripts.swarm.store import SwarmError

FRESHNESS = """query($url: URI!) { resource(url: $url) { ... on PullRequest {
    repository { nameWithOwner }
    baseRefName baseRef { branchProtectionRule { requiredStatusCheckContexts requiredStatusChecks { context app { databaseId } } } }
} } }"""
FIELDS = 'id state headRefOid baseRefName mergeQueueEntry { id position state } isDraft reviewDecision statusCheckRollup { state } repository { mergeQueue(branch: "dev") { id } }'
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
    command = ["gh", "api", path]
    try:
        done = run(command, capture_output=True, text=True, timeout=20)
        if done.returncode:
            raise SwarmError(done.stderr.strip() or "GitHub API failed")
        return done.stdout if path.endswith("/logs") else json.loads(done.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise SwarmError(f"GitHub API: {exc}") from exc


def require_green(url: str, head: str, run) -> None:
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
    pull = ledger_events.view(url, read, required_checks=expected)
    if pull is None or pull.head != head or not pull.resolved or pull.red:
        failed = f"; failed: {', '.join(pull.failed)}" if pull and pull.failed else ""
        raise SwarmError(f"required checks on the current pull request head must pass before queueing{failed}")


def operate(action: str, url: str, run=subprocess.run, admit=None) -> dict:
    raw = graphql(STATE, {"url": url}, run)["resource"]
    if not raw:
        raise SwarmError("GitHub URL does not identify a pull request")
    if action != "state" and raw["baseRefName"] != "dev":
        raise SwarmError("swarm merge queue operations require a pull request into dev")
    if action == "queue" and raw["mergeQueueEntry"] is None:
        if admit is not None:
            admit(raw["headRefOid"])
        if raw["repository"]["mergeQueue"] is None:
            if raw["isDraft"] or raw["reviewDecision"] not in (None, "APPROVED"):
                raise SwarmError("squash merge requires a ready pull request with green checks and no pending review")
            require_green(url, raw["headRefOid"], run)
            graphql(MERGE, {"id": raw["id"], "head": raw["headRefOid"]}, run)
        else:
            require_green(url, raw["headRefOid"], run)
            graphql(QUEUE, {"id": raw["id"], "head": raw["headRefOid"]}, run)
        raw = graphql(STATE, {"url": url}, run)["resource"]
    if action == "dequeue":
        graphql(DEQUEUE, {"id": raw["id"]}, run)
        raw = graphql(STATE, {"url": url}, run)["resource"]
    return {
        "url": url,
        "state": raw["state"],
        "head": raw["headRefOid"],
        "queued": raw["mergeQueueEntry"] is not None,
        "entry": raw["mergeQueueEntry"],
    }
