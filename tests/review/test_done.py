import argparse
from types import SimpleNamespace
from typing import Any, NoReturn, cast

import pytest

from scripts.swarm import cli
from scripts.swarm.store import RedisStore, SwarmError
from scripts.swarm_ledger import ledger_kinds
from tests.review.fakes import REVIEWER, SLUG, store

TASK = "r1"
REFUSAL = (
    "a review task is done only after it posts at least one finding: "
    f"agentihooks review post <url> --slug {SLUG} --finding <text>"
)


class Reached(Exception):
    """Raised by the stubbed proof check to show swarm done got past the review refusal."""


class Untouched:
    """A swarm store that fails the test on any read."""

    def __getattr__(self, name: str) -> NoReturn:
        raise AssertionError(f"swarm done read the store attribute {name}")


def _done(monkeypatch: pytest.MonkeyPatch, found: object, lane: str, kind: str) -> None:
    agent = SimpleNamespace(name=REVIEWER, lane=lane, task=TASK)
    monkeypatch.setattr(cli, "_worker", lambda *args: agent)
    monkeypatch.setattr(cli.done_gate, "require_local", lambda *args: None)
    monkeypatch.setattr(cli, "LedgerClient", lambda: SimpleNamespace(task=lambda *args: {"kind": kind}))

    def reached(task: dict[str, Any]) -> NoReturn:
        raise Reached(task)

    monkeypatch.setattr(cli.ledger_kinds, "unmet", reached)
    fields: dict[str, Any] = {f"proof_{key}": "" for key in ledger_kinds.PROOF_KEYS}
    cli.cmd_done(cast(RedisStore, found), argparse.Namespace(slug=SLUG, pr="", **fields))


def _count(found: RedisStore, slug: str, task: str) -> None:
    found.redis.hincrby(found.key(slug, "review-tasks"), task, 1)


@pytest.mark.feature("review-done-findings")
@pytest.mark.parametrize(
    ("lane", "kind"),
    [("eng", "code"), ("eng", ""), ("ci", "ci"), ("eng", "ops"), ("plan", "plan"), ("eng", "research")],
)
def test_swarm_done_never_reads_the_review_record_for_other_lanes_and_kinds(
    monkeypatch: pytest.MonkeyPatch, lane: str, kind: str
) -> None:
    with pytest.raises(Reached):
        _done(monkeypatch, Untouched(), lane, kind)


@pytest.mark.feature("review-done-findings")
@pytest.mark.parametrize(("lane", "kind"), [("review", "code"), ("review", "review"), ("eng", "review")])
def test_swarm_done_refuses_a_review_task_that_posted_no_finding(
    monkeypatch: pytest.MonkeyPatch, lane: str, kind: str
) -> None:
    with pytest.raises(SwarmError) as refused:
        _done(monkeypatch, store(), lane, kind)
    assert str(refused.value) == REFUSAL


@pytest.mark.feature("review-done-findings")
@pytest.mark.parametrize(("lane", "kind"), [("review", "code"), ("review", "review"), ("eng", "review")])
def test_one_posted_finding_lets_a_review_task_reach_the_proof_check(
    monkeypatch: pytest.MonkeyPatch, lane: str, kind: str
) -> None:
    found = store()
    _count(found, SLUG, TASK)
    with pytest.raises(Reached):
        _done(monkeypatch, found, lane, kind)


@pytest.mark.feature("review-done-findings")
def test_the_count_is_read_for_the_exact_swarm_and_task(monkeypatch: pytest.MonkeyPatch) -> None:
    found = store()
    _count(found, "other", TASK)
    _count(found, SLUG, "r2")
    with pytest.raises(SwarmError) as refused:
        _done(monkeypatch, found, "review", "review")
    assert str(refused.value) == REFUSAL
