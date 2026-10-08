import json
import subprocess

from scripts.swarm.store import SwarmError

FIELDS = "id state headRefOid baseRefName mergeQueueEntry { id position state }"
STATE = "query($url: URI!) { resource(url: $url) { ... on PullRequest { " + FIELDS + " } } }"
QUEUE = """mutation($id: ID!, $head: GitObjectID!) {
    enqueuePullRequest(input: {pullRequestId: $id, expectedHeadOid: $head}) { mergeQueueEntry { id } }
}"""

DEQUEUE = """mutation($id: ID!) {
    dequeuePullRequest(input: {id: $id}) { mergeQueueEntry { id } }
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


def operate(action: str, url: str, run=subprocess.run) -> dict:
    raw = graphql(STATE, {"url": url}, run)["resource"]
    if not raw:
        raise SwarmError("GitHub URL does not identify a pull request")
    if action != "state" and raw["baseRefName"] != "dev":
        raise SwarmError("swarm merge queue operations require a pull request into dev")
    if action == "queue":
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
