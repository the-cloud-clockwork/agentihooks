import re

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


def require_unqueued(remote: str, branch: str) -> None:
    repository = re.fullmatch(r"(?:https?://|ssh://git@|git@)github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?", remote)
    if repository is None:
        return
    owner, name = repository.groups()
    data = merge_queue.graphql(QUERY, {"owner": owner, "name": name, "branch": branch})
    pulls = data["repository"]["pullRequests"]
    if pulls["pageInfo"]["hasNextPage"]:
        raise SwarmError("cannot verify every pull request for the publication branch; nothing pushed")
    for pull in pulls["nodes"]:
        if pull["mergeQueueEntry"] is not None:
            raise SwarmError(
                f"branch {branch} has a pull request in the merge queue: {pull['url']}; dequeue it before publishing"
            )
