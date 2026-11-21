"""The review record: findings posted through the forge per change request head, the author's marks and review done."""

from types import SimpleNamespace
from typing import cast

import pytest

from scripts.review import record
from scripts.swarm.store import RedisStore, SwarmError
from tests.review.fakes import AUTHOR, FEATURE, HEAD, REVIEWER, SLUG, URL, FakeForge, store

pytestmark = pytest.mark.unit


def _posted(found: record.Record) -> list[tuple[str, str, str, str, str, str]]:
    return [(item.id, item.body, item.comment, item.by, item.task, item.state) for item in found.findings]


@pytest.mark.feature(FEATURE)
def test_a_post_stores_each_finding_with_its_forge_comment_on_the_reported_head() -> None:
    swarm, forge = store(), FakeForge()
    posted = record.post(swarm, SLUG, forge, URL, REVIEWER, "r1", ["Null head is not handled", "No test"])
    assert forge.posts == [(7, "Null head is not handled"), (7, "No test")]
    expected = [
        ("f1", "Null head is not handled", f"{URL}#issuecomment-1", REVIEWER, "r1", "open"),
        ("f2", "No test", f"{URL}#issuecomment-2", REVIEWER, "r1", "open"),
    ]
    assert (posted.url, posted.head, _posted(posted)) == (URL, HEAD, expected)
    assert record.read(swarm, SLUG, URL, HEAD) == posted
    assert record.read(swarm, SLUG, URL, "another-head") is None


@pytest.mark.feature(FEATURE)
@pytest.mark.parametrize("bodies", [[], ["", "   "]])
def test_a_post_without_a_finding_is_refused_and_posts_nothing(bodies: list[str]) -> None:
    swarm, forge = store(), FakeForge()
    with pytest.raises(SwarmError, match="^a review posts at least one finding$"):
        record.post(swarm, SLUG, forge, URL, REVIEWER, "r1", bodies)
    assert forge.posts == []
    assert record.read(swarm, SLUG, URL, HEAD) is None


@pytest.mark.feature(FEATURE)
def test_a_second_post_on_the_same_head_appends_numbered_findings() -> None:
    swarm, forge = store(), FakeForge()
    record.post(swarm, SLUG, forge, URL, REVIEWER, "r1", ["First"])
    posted = record.post(swarm, SLUG, forge, URL, "reviewer@a1b2c3-0003", "r2", ["Second"])
    assert [(item.id, item.body, item.by) for item in posted.findings] == [
        ("f1", "First", REVIEWER),
        ("f2", "Second", "reviewer@a1b2c3-0003"),
    ]


@pytest.mark.feature(FEATURE)
def test_a_record_expires_thirty_days_after_its_last_write() -> None:
    swarm = store()
    record.post(swarm, SLUG, FakeForge(), URL, REVIEWER, "r1", ["First"])
    keys = [key for key in swarm.redis.keys("*") if ":review" in key]
    assert len(keys) == 2
    assert all(0 < swarm.redis.ttl(key) <= record.TTL_SEC == 30 * 24 * 3600 for key in keys)


@pytest.mark.feature(FEATURE)
def test_a_change_request_url_without_a_number_is_refused() -> None:
    with pytest.raises(SwarmError, match="^not a change request URL: https://github.com/o/r$"):
        record.post(store(), SLUG, FakeForge(), "https://github.com/o/r", REVIEWER, "r1", ["First"])


@pytest.mark.feature(FEATURE)
def test_the_author_marks_findings_read_or_answered_on_the_current_head() -> None:
    swarm, forge = store(), FakeForge()
    record.post(swarm, SLUG, forge, URL, REVIEWER, "r1", ["First", "Second"])
    read = record.mark(swarm, SLUG, forge, URL, AUTHOR, ["f1"], "")
    assert [(item.id, item.state, item.answer) for item in read.findings] == [("f1", "read", ""), ("f2", "open", "")]
    answered = record.mark(swarm, SLUG, forge, URL, AUTHOR, [], "Fixed in the next head")
    assert [(item.state, item.answer) for item in answered.findings] == [
        ("answered", "Fixed in the next head"),
        ("answered", "Fixed in the next head"),
    ]
    assert record.read(swarm, SLUG, URL, HEAD) == answered
    assert answered.unread() == ()


@pytest.mark.feature(FEATURE)
def test_marking_an_unknown_finding_or_a_head_without_review_is_refused() -> None:
    swarm, forge = store(), FakeForge()
    with pytest.raises(SwarmError, match=f"^no review on head {HEAD} of {URL}$"):
        record.mark(swarm, SLUG, forge, URL, AUTHOR, [], "")
    record.post(swarm, SLUG, forge, URL, REVIEWER, "r1", ["First"])
    with pytest.raises(SwarmError, match=f"^no finding f9 on head {HEAD} of {URL}$"):
        record.mark(swarm, SLUG, forge, URL, AUTHOR, ["f9"], "")


@pytest.mark.feature(FEATURE)
def test_a_reviewer_cannot_mark_its_own_findings_read() -> None:
    swarm, forge = store(), FakeForge()
    record.post(swarm, SLUG, forge, URL, REVIEWER, "r1", ["First"])
    with pytest.raises(SwarmError, match="^a reviewer cannot mark its own findings read: f1$"):
        record.mark(swarm, SLUG, forge, URL, REVIEWER, [], "")
    found = record.read(swarm, SLUG, URL, HEAD)
    assert found is not None
    assert [item.state for item in found.findings] == ["open"]


@pytest.mark.feature(FEATURE)
def test_review_done_with_no_findings_is_refused() -> None:
    refused = record.done_refusal(store(), SLUG, "review", "r1")
    assert refused == (
        "a review task is done only after it posts at least one finding: "
        "agentihooks review post <url> --slug <slug> --finding <text>"
    )


@pytest.mark.feature(FEATURE)
def test_review_done_with_one_finding_passes() -> None:
    swarm = store()
    record.post(swarm, SLUG, FakeForge(), URL, REVIEWER, "r1", ["First"])
    assert record.done_refusal(swarm, SLUG, "review", "r1") is None
    assert record.done_refusal(swarm, SLUG, "review", "r2") is not None


@pytest.mark.feature(FEATURE)
@pytest.mark.parametrize("kind", ["code", "ci", "ops", "plan"])
def test_other_task_kinds_never_read_review_records(kind: str) -> None:
    unreadable = cast(RedisStore, SimpleNamespace(redis=None, key=None))
    assert record.done_refusal(unreadable, SLUG, kind, "t1") is None
