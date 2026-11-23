"""The review record: findings posted through the forge, stored per change request head."""

import pytest

from scripts.review import record
from scripts.swarm.store import SwarmError
from tests.review.fakes import HEAD, REVIEWER, SLUG, URL, FakeForge, store

pytestmark = pytest.mark.unit


def _posted(found: record.Record) -> list[tuple[str, str, str, str, str]]:
    return [(item.id, item.body, item.comment, item.by, item.task) for item in found.findings]


@pytest.mark.feature("review-record-post")
def test_a_post_stores_each_finding_with_its_forge_comment_on_the_reported_head() -> None:
    swarm, forge = store(), FakeForge()
    posted = record.post(swarm, SLUG, forge, URL, REVIEWER, "r1", ["Null head is not handled", " No test "])
    assert forge.reads == [7]
    assert forge.posts == [(7, "Null head is not handled"), (7, "No test")]
    expected = [
        ("f1", "Null head is not handled", f"{URL}#issuecomment-1", REVIEWER, "r1"),
        ("f2", "No test", f"{URL}#issuecomment-2", REVIEWER, "r1"),
    ]
    assert (posted.url, posted.head, _posted(posted)) == (URL, HEAD, expected)
    assert [(item.state, item.answer, item.read_by) for item in posted.findings] == [("open", "", ())] * 2
    assert record.read(swarm, SLUG, URL, HEAD) == posted
    assert record.read(swarm, SLUG, URL, "another-head") is None


@pytest.mark.feature("review-record-post")
@pytest.mark.parametrize("bodies", [[], ["", "   "]])
def test_a_post_without_a_finding_is_refused_and_posts_nothing(bodies: list[str]) -> None:
    swarm, forge = store(), FakeForge()
    with pytest.raises(SwarmError, match="^a review posts at least one finding$"):
        record.post(swarm, SLUG, forge, URL, REVIEWER, "r1", bodies)
    assert (forge.reads, forge.posts) == ([], [])
    assert record.read(swarm, SLUG, URL, HEAD) is None
    assert swarm.redis.keys("*") == []


@pytest.mark.feature("review-record-post")
def test_a_second_post_on_the_same_head_appends_numbered_findings() -> None:
    swarm, forge = store(), FakeForge()
    record.post(swarm, SLUG, forge, URL, REVIEWER, "r1", ["First"])
    posted = record.post(swarm, SLUG, forge, URL, "reviewer@a1b2c3-0003", "r2", ["Second"])
    assert _posted(posted) == [
        ("f1", "First", f"{URL}#issuecomment-1", REVIEWER, "r1"),
        ("f2", "Second", f"{URL}#issuecomment-2", "reviewer@a1b2c3-0003", "r2"),
    ]
    assert record.read(swarm, SLUG, URL, HEAD) == posted


@pytest.mark.feature("review-record-post")
def test_a_new_head_starts_its_own_findings() -> None:
    swarm = store()
    record.post(swarm, SLUG, FakeForge(), URL, REVIEWER, "r1", ["First"])
    posted = record.post(swarm, SLUG, FakeForge(head="fed654"), URL, REVIEWER, "r1", ["Second"])
    assert (posted.head, [(item.id, item.body) for item in posted.findings]) == ("fed654", [("f1", "Second")])
    first = record.read(swarm, SLUG, URL, HEAD)
    assert first is not None
    assert [item.body for item in first.findings] == ["First"]


@pytest.mark.feature("review-record-post")
def test_a_record_expires_thirty_days_after_its_last_write() -> None:
    swarm = store()
    record.post(swarm, SLUG, FakeForge(), URL, REVIEWER, "r1", ["First"])
    keys = sorted(key for key in swarm.redis.keys("*") if ":review" in key)
    assert len(keys) == 2
    assert record.TTL_SEC == 30 * 24 * 3600
    assert all(record.TTL_SEC - 60 < swarm.redis.ttl(key) <= record.TTL_SEC for key in keys)
    for key in keys:
        swarm.redis.expire(key, 5)
    record.post(swarm, SLUG, FakeForge(), URL, REVIEWER, "r1", ["Second"])
    assert all(record.TTL_SEC - 60 < swarm.redis.ttl(key) <= record.TTL_SEC for key in keys)


@pytest.mark.feature("review-record-post")
def test_a_change_request_url_without_a_number_is_refused() -> None:
    forge = FakeForge()
    with pytest.raises(SwarmError, match="^not a change request URL: https://github.com/o/r$"):
        record.post(store(), SLUG, forge, "https://github.com/o/r", REVIEWER, "r1", ["First"])
    assert forge.posts == []


@pytest.mark.feature("review-record-post")
def test_each_posted_finding_counts_once_for_its_task() -> None:
    swarm, forge = store(), FakeForge()
    key = swarm.key(SLUG, "review-tasks")
    record.post(swarm, SLUG, forge, URL, REVIEWER, "r1", ["First", "Second"])
    record.post(swarm, SLUG, forge, URL, "reviewer@a1b2c3-0003", "r2", ["Third"])
    assert swarm.redis.hgetall(key) == {"r1": "2", "r2": "1"}


@pytest.mark.feature("review-record-post")
def test_a_post_without_a_task_counts_nothing() -> None:
    swarm = store()
    record.post(swarm, SLUG, FakeForge(), URL, REVIEWER, "", ["First"])
    assert swarm.redis.hgetall(swarm.key(SLUG, "review-tasks")) == {}
    found = record.read(swarm, SLUG, URL, HEAD)
    assert found is not None
    assert [item.task for item in found.findings] == [""]
