from urllib.parse import urlsplit

from scripts.swarm import merge_queue
from scripts.swarm.store import SwarmError

QUERY = """query($owner: String!, $name: String!, $branch: String!) {
  repository(owner: $owner, name: $name) {
    pullRequests(headRefName: $branch, states: OPEN, first: 100) {
      nodes { url mergeQueueEntry { id } }
      pageInfo { hasNextPage }
    }
  }
}"""
HEAD = """query($url: URI!) { resource(url: $url) { ... on PullRequest {
  state headRefName isCrossRepository repository { nameWithOwner }
} } }"""


def github(remote: str) -> tuple[str, str] | None:
    parsed = urlsplit(remote if "://" in remote else f"ssh://{remote.replace(':', '/')}")
    if parsed.hostname not in {"github.com", "ssh.github.com"}:
        return None
    owner, name = parsed.path.strip("/").removesuffix(".git").split("/")
    return owner, name


def open_head(remote: str, url: str) -> str:
    repository = github(remote)
    if repository is None or not url:
        return ""
    pull = merge_queue.graphql(HEAD, {"url": url})["resource"] or {}
    if pull.get("state") != "OPEN" or pull["isCrossRepository"]:
        return ""
    if pull["repository"]["nameWithOwner"].lower() != "/".join(repository).lower():
        return ""
    return pull["headRefName"]


def require_unqueued(remote: str, branch: str) -> None:
    repository = github(remote)
    if repository is None:
        return
    owner, name = repository
    data = merge_queue.graphql(QUERY, {"owner": owner, "name": name, "branch": branch})
    pulls = data["repository"]["pullRequests"]
    if pulls["pageInfo"]["hasNextPage"]:
        raise SwarmError("cannot verify every pull request for the publication branch; nothing pushed")
    for pull in pulls["nodes"]:
        if pull["mergeQueueEntry"] is not None:
            raise SwarmError(
                f"branch {branch} has a pull request in the merge queue: {pull['url']}; dequeue it before publishing"
            )
