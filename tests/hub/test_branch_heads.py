from functools import partial
from typing import Any

import pytest

from scripts.handoff import envelope
from scripts.hub import branch_heads
from scripts.swarm import continuation, merge_queue, stack
from scripts.swarm.store import SwarmConfig, SwarmError

ORIGIN = "github.com/acme/app"
QUERY = (
    "query($owner: String!, $name: String!, $b0: String!, $b1: String!) { repository(owner: $owner, name: $name) "
    "{ b0: ref(qualifiedName: $b0) { target { oid } } b1: ref(qualifiedName: $b1) { target { oid } } } }"
)


def _graphql(monkeypatch: pytest.MonkeyPatch, answer: Any) -> list[tuple[str, dict[str, str], Any]]:
    asked: list[tuple[str, dict[str, str], Any]] = []

    def graphql(query: str, variables: dict[str, str], run: Any = None) -> dict[str, Any]:
        asked.append((query, variables, run))
        if isinstance(answer, Exception):
            raise answer
        return {"repository": answer}

    monkeypatch.setattr(merge_queue, "graphql", graphql)
    return asked


def test_heads_reads_every_branch_once_in_one_query(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = _graphql(monkeypatch, {"b0": {"target": {"oid": "abc"}}, "b1": None})
    run = object()

    found = branch_heads.heads(ORIGIN, ["kept", "gone", "kept"], run)

    assert found == {"kept": "abc", "gone": "none"}
    assert asked == [(QUERY, {"owner": "acme", "name": "app", "b0": "refs/heads/kept", "b1": "refs/heads/gone"}, run)]


def test_heads_asks_nothing_for_no_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = _graphql(monkeypatch, {})

    assert branch_heads.heads(ORIGIN, []) == {}
    assert asked == []


@pytest.mark.parametrize("answer", [SwarmError("GitHub API failed"), None])
def test_heads_marks_every_branch_unknown_when_github_is_not_read(monkeypatch: pytest.MonkeyPatch, answer: Any) -> None:
    _graphql(monkeypatch, answer)

    assert branch_heads.heads(ORIGIN, ["a", "b"]) == {"a": "unknown", "b": "unknown"}


def test_a_stored_origin_reclaims_an_earlier_life_branch_through_github(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = _graphql(monkeypatch, {"b0": {"target": {"oid": "abc"}}})
    reclaim, origin = branch_heads.reclaimer(SwarmConfig("sw", "/repo", 1, 1, repo_origin=ORIGIN))

    found = reclaim("/repo", ["engineer@a1-0001"], "")

    assert origin == ORIGIN
    assert found == {
        "remote_branch": "engineer-a1-0001",
        "remote_head": "abc",
        "continue_from": "origin/engineer-a1-0001",
        "fresh_reason": "none",
    }
    assert asked[0][1] == {"owner": "acme", "name": "app", "b0": "refs/heads/engineer-a1-0001"}


def test_a_stored_origin_skips_a_forbidden_recorded_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    _graphql(monkeypatch, {"b0": {"target": {"oid": "abc"}}})
    reclaim, _ = branch_heads.reclaimer(SwarmConfig("sw", "/repo", 1, 1, repo_origin=ORIGIN))

    found = reclaim("/repo", [], "taken", forbidden={"taken"})

    assert found["continue_from"] == "fresh"


def test_no_stored_origin_reads_the_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(envelope, "remote_heads", lambda repo: {"kept": f"head of {repo}"})
    monkeypatch.setattr(stack, "origin_key", lambda repo: f"key of {repo}")

    reclaim, origin = branch_heads.reclaimer(SwarmConfig("sw", "/repo", 1, 1))

    assert origin == "key of /repo"
    assert isinstance(reclaim, partial)
    assert reclaim("/repo", [], "kept")["remote_head"] == "head of /repo"


def test_resolve_compares_branches_against_a_given_origin_without_a_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stack, "origin_key", lambda repo: pytest.fail("a given origin needs no checkout"))
    task = {"id": "t1"}
    other = {"id": "t2", "branch": "engineer-a1-0001", "pr_url": f"https://{ORIGIN}/pull/2"}
    heads = {"engineer-a1-0001": "abc"}

    found = continuation.resolve(
        "/repo",
        task,
        {"t1": task, "t2": other},
        ["engineer@a1-0001"],
        {},
        partial(envelope.reclaim, heads=heads),
        ORIGIN,
    )

    assert found["continue_from"] == "fresh"
