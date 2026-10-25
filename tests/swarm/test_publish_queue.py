import pytest

from scripts.swarm import merge_queue, publish_queue
from scripts.swarm.store import SwarmError

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "remote",
    [
        "git@github.com:example/repo.git",
        "ssh://git@github.com/example/repo.git",
        "ssh://git@github.com:22/example/repo.git",
        "ssh://git@ssh.github.com:443/example/repo.git",
        "git://github.com/example/repo.git",
        "https://GitHub.com/example/repo.git",
        "https://github.com/example/repo.git",
        "https://github.com/example/repo",
    ],
)
@pytest.mark.parametrize("queued", [False, True])
def test_publication_queue_admission_queries_the_exact_branch(monkeypatch, remote, queued):
    calls = []

    def query(document, variables):
        assert (
            document
            == """query($owner: String!, $name: String!, $branch: String!) {
  repository(owner: $owner, name: $name) {
    pullRequests(headRefName: $branch, states: OPEN, first: 100) {
      nodes { url mergeQueueEntry { id } }
      pageInfo { hasNextPage }
    }
  }
}"""
        )
        assert variables == {"owner": "example", "name": "repo", "branch": "stacked"}
        calls.append(variables)
        return {
            "repository": {
                "pullRequests": {
                    "nodes": [
                        {"url": "https://github.com/example/repo/pull/7", "mergeQueueEntry": None},
                        {"url": "https://github.com/example/repo/pull/8", "mergeQueueEntry": {} if queued else None},
                    ],
                    "pageInfo": {"hasNextPage": False},
                }
            }
        }

    monkeypatch.setattr(merge_queue, "graphql", query)
    if queued:
        with pytest.raises(SwarmError) as refused:
            publish_queue.require_unqueued(remote, "stacked")
        assert str(refused.value) == (
            "branch stacked has a pull request in the merge queue: "
            "https://github.com/example/repo/pull/8; dequeue it before publishing"
        )
    else:
        publish_queue.require_unqueued(remote, "stacked")
    assert len(calls) == 1


@pytest.mark.parametrize("remote", ["/repo/remote.git", "git@gitlab.com:example/repo.git"])
def test_non_github_publication_does_not_query_github(monkeypatch, remote):
    def unexpected(*args):
        pytest.fail("non GitHub remote queried GitHub")

    monkeypatch.setattr(merge_queue, "graphql", unexpected)
    publish_queue.require_unqueued(remote, "stacked")


@pytest.mark.parametrize("more", [False, True])
def test_queue_admission_requires_a_complete_pull_request_census(monkeypatch, more):
    monkeypatch.setattr(
        merge_queue,
        "graphql",
        lambda *args: {"repository": {"pullRequests": {"nodes": [], "pageInfo": {"hasNextPage": more}}}},
    )
    if more:
        with pytest.raises(SwarmError) as refused:
            publish_queue.require_unqueued("https://github.com/example/repo", "stacked")
        assert str(refused.value) == "cannot verify every pull request for the publication branch; nothing pushed"
    else:
        publish_queue.require_unqueued("https://github.com/example/repo", "stacked")


def test_queue_read_failure_refuses_publication(monkeypatch):
    def unavailable(*args):
        raise SwarmError("GitHub API unavailable")

    monkeypatch.setattr(merge_queue, "graphql", unavailable)
    with pytest.raises(SwarmError, match="GitHub API unavailable"):
        publish_queue.require_unqueued("https://github.com/example/repo", "stacked")


def test_queue_query_preserves_repository_name_characters(monkeypatch):
    calls = []

    def query(document, variables):
        assert variables == {"owner": "Xowner", "name": "repoX", "branch": "stacked"}
        calls.append(variables)
        return {"repository": {"pullRequests": {"nodes": [], "pageInfo": {"hasNextPage": False}}}}

    monkeypatch.setattr(merge_queue, "graphql", query)
    publish_queue.require_unqueued("git@github.com:Xowner/repoX.git", "stacked")
    assert len(calls) == 1
