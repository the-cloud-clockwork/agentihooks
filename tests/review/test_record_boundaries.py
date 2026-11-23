import json
from hashlib import sha256
from unittest.mock import Mock

import pytest

from scripts.forge.model import ChangeRequest
from scripts.review import record
from scripts.swarm.store import SwarmError
from tests.review.fakes import AUTHOR, HEAD, REVIEWER, SLUG, URL, FakeForge, store

pytestmark = pytest.mark.unit


def _target(monkeypatch: pytest.MonkeyPatch, url: str) -> FakeForge:
    forge = FakeForge()
    monkeypatch.setattr(forge, "read", Mock(return_value=ChangeRequest(7, url, "Title", "Body", HEAD, "", (), ())))
    return forge


@pytest.mark.feature("review-findings-queue-gate")
def test_findings_are_isolated_by_swarm_and_change_request(monkeypatch: pytest.MonkeyPatch) -> None:
    found = store()
    record.post(found, SLUG, FakeForge(), URL, REVIEWER, "r1", ["First"])
    record.post(found, "other", FakeForge(), URL, REVIEWER, "r2", ["Second"])
    other_url = URL.replace("/pull/7", "/pull/8")
    record.post(found, SLUG, _target(monkeypatch, other_url), other_url, REVIEWER, "r3", ["Third"])
    for slug, url, body in [(SLUG, URL, "First"), ("other", URL, "Second"), (SLUG, other_url, "Third")]:
        saved = record.read(found, slug, url, HEAD)
        assert saved is not None
        assert (saved.url, saved.head, [item.body for item in saved.findings]) == (url, HEAD, [body])


@pytest.mark.feature("review-findings-queue-gate")
def test_a_previously_persisted_record_can_be_read() -> None:
    found = store()
    assert found.redis is not None
    saved = record.Record(URL, HEAD, (record.Finding("f1", "First", URL, REVIEWER, "r1"),))
    key = found.key(SLUG, "review", sha256(URL.encode()).hexdigest()[:16])
    found.redis.hset(key, HEAD, json.dumps(record.document(saved)))
    assert record.read(found, SLUG, URL, HEAD) == saved


@pytest.mark.feature("review-findings-queue-gate")
@pytest.mark.parametrize(("requested", "reported"), [(URL + "/", URL), (URL, URL + "/")])
def test_trailing_slashes_do_not_change_the_forge_target(
    monkeypatch: pytest.MonkeyPatch, requested: str, reported: str
) -> None:
    forge = _target(monkeypatch, reported)
    saved = record.post(store(), SLUG, forge, requested, REVIEWER, "r1", ["First"])
    assert (saved.url, saved.head, forge.posts) == (requested, HEAD, [(7, "First")])


@pytest.mark.feature("review-findings-queue-gate")
def test_a_forge_url_with_a_suffix_is_a_different_request(monkeypatch: pytest.MonkeyPatch) -> None:
    forge = _target(monkeypatch, URL + "X")
    with pytest.raises(SwarmError) as error:
        record.post(store(), SLUG, forge, URL, REVIEWER, "r1", ["First"])
    assert str(error.value) == f"change request {URL} does not match forge target {URL}X"
    assert forge.posts == []


@pytest.mark.feature("review-findings-queue-gate")
def test_a_numeric_looking_request_suffix_is_refused() -> None:
    assert record.number_of("7") == 7
    with pytest.raises(SwarmError) as error:
        record.number_of(URL + "X")
    assert str(error.value) == f"not a change request URL: {URL}X"


@pytest.mark.feature("review-findings-queue-gate")
def test_successful_posts_are_counted_once_and_reads_do_not_add_posts() -> None:
    found, forge = store(), FakeForge()
    assert found.redis is not None
    key = found.key(SLUG, "review-tasks")
    record.post(found, SLUG, forge, URL, REVIEWER, "r1", ["First", "Second"])
    record.post(found, SLUG, forge, URL, "another@sw", "r2", ["Third"])
    assert found.redis.hgetall(key) == {"r1": "2", "r2": "1"}
    record.mark(found, SLUG, forge, URL, AUTHOR, [], "")
    assert found.redis.hgetall(key) == {"r1": "2", "r2": "1"}


@pytest.mark.feature("review-findings-queue-gate")
def test_multiple_invalid_or_self_authored_findings_are_named() -> None:
    found, forge = store(), FakeForge()
    record.post(found, SLUG, forge, URL, REVIEWER, "r1", ["First", "Second"])
    with pytest.raises(SwarmError) as unknown:
        record.mark(found, SLUG, forge, URL, AUTHOR, ["f9", "f8"], "")
    assert str(unknown.value) == f"no finding f8, f9 on head {HEAD} of {URL}"
    with pytest.raises(SwarmError) as own:
        record.mark(found, SLUG, forge, URL, REVIEWER, [], "")
    assert str(own.value) == "a reviewer cannot mark its own findings read: f1, f2"


@pytest.mark.feature("review-findings-queue-gate")
def test_completion_requires_a_positive_count_for_the_exact_swarm_and_task() -> None:
    found = store()
    assert found.redis is not None
    found.redis.hset(found.key(SLUG, "review-tasks"), "r1", "0")
    reason = (
        "a review task is done only after it posts at least one finding: "
        "agentihooks review post <url> --slug <slug> --finding <text>"
    )
    with pytest.raises(SwarmError) as error:
        record.require_done(found, SLUG, "review", "r1")
    assert str(error.value) == reason
    record.post(found, SLUG, FakeForge(), URL, REVIEWER, "r1", ["First"])
    assert record.require_done(found, SLUG, "review", "r1") is None
    for slug, task in [(SLUG, "r2"), ("other", "r1")]:
        with pytest.raises(SwarmError) as error:
            record.require_done(found, slug, "review", task)
        assert str(error.value) == reason
    assert record.require_done(found, SLUG, "code", "r2") is None
