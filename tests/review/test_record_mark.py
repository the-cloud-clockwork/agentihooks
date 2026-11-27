"""The author marks the review findings on a change request's current head read or answered."""

import pytest

from scripts.review import record
from scripts.swarm.store import RedisStore, SwarmError
from tests.review.fakes import HEAD, REVIEWER, SLUG, URL, FakeForge, store

pytestmark = pytest.mark.unit

AUTHOR = "engineer@a1b2c3-0001"
SECOND = "engineer@a1b2c3-0003"
MOVED = "fed654cba321"


def _states(found: record.Record | None) -> list[tuple[str, str, str, tuple[str, ...]]]:
    assert found is not None
    return [(item.id, item.state, item.answer, item.read_by) for item in found.findings]


def _posted(bodies: list[str]) -> tuple[RedisStore, FakeForge]:
    swarm, forge = store(), FakeForge()
    record.post(swarm, SLUG, forge, URL, REVIEWER, "r1", bodies)
    return swarm, forge


@pytest.mark.feature("review-findings-read")
def test_the_author_marks_findings_read_or_answered_on_the_current_head() -> None:
    swarm, forge = _posted(["First", "Second"])
    read = record.mark(swarm, SLUG, forge, URL, AUTHOR, ["f1"], "")
    assert (read.url, read.head) == (URL, HEAD)
    assert _states(read) == [("f1", "read", "", (AUTHOR,)), ("f2", "open", "", ())]
    answered = record.mark(swarm, SLUG, forge, URL, AUTHOR, [], "Fixed in the next head")
    assert _states(answered) == [
        ("f1", "answered", "Fixed in the next head", (AUTHOR,)),
        ("f2", "answered", "Fixed in the next head", (AUTHOR,)),
    ]
    assert record.read(swarm, SLUG, URL, HEAD) == answered


@pytest.mark.feature("review-findings-read")
def test_marking_an_unknown_finding_or_a_head_without_review_is_refused() -> None:
    swarm, forge = store(), FakeForge()
    with pytest.raises(SwarmError) as empty:
        record.mark(swarm, SLUG, forge, URL, AUTHOR, [], "")
    assert str(empty.value) == f"no review on head {HEAD} of {URL}"
    assert record.read(swarm, SLUG, URL, HEAD) is None
    record.post(swarm, SLUG, forge, URL, REVIEWER, "r1", ["First"])
    with pytest.raises(SwarmError) as unknown:
        record.mark(swarm, SLUG, forge, URL, AUTHOR, ["f9", "f1", "f8"], "")
    assert str(unknown.value) == f"no finding f8, f9 on head {HEAD} of {URL}"
    with pytest.raises(SwarmError) as stale:
        record.mark(swarm, SLUG, FakeForge(head=MOVED), URL, AUTHOR, [], "")
    assert str(stale.value) == f"no review on head {MOVED} of {URL}"
    assert record.read(swarm, SLUG, URL, MOVED) is None
    assert _states(record.read(swarm, SLUG, URL, HEAD)) == [("f1", "open", "", ())]


@pytest.mark.feature("review-findings-read")
def test_a_reviewer_cannot_mark_its_own_findings() -> None:
    swarm, forge = _posted(["First"])
    record.post(swarm, SLUG, forge, URL, SECOND, "r2", ["Second", "Third"])
    with pytest.raises(SwarmError) as every:
        record.mark(swarm, SLUG, forge, URL, REVIEWER, [], "")
    assert str(every.value) == "a reviewer cannot mark its own findings read: f1"
    with pytest.raises(SwarmError) as chosen:
        record.mark(swarm, SLUG, forge, URL, SECOND, ["f3", "f1", "f2"], "Done")
    assert str(chosen.value) == "a reviewer cannot mark its own findings read: f2, f3"
    assert _states(record.read(swarm, SLUG, URL, HEAD)) == [
        ("f1", "open", "", ()),
        ("f2", "open", "", ()),
        ("f3", "open", "", ()),
    ]
    others = record.mark(swarm, SLUG, forge, URL, REVIEWER, ["f2"], "")
    assert _states(others)[:2] == [("f1", "open", "", ()), ("f2", "read", "", (REVIEWER,))]


@pytest.mark.feature("review-findings-read")
def test_a_read_keeps_the_calling_author_once_beside_earlier_readers() -> None:
    swarm, forge = _posted(["First"])
    answered = record.mark(swarm, SLUG, forge, URL, AUTHOR, [], "Fixed")
    assert _states(answered) == [("f1", "answered", "Fixed", (AUTHOR,))]
    record.mark(swarm, SLUG, forge, URL, SECOND, ["f1"], "Agreed")
    again = record.mark(swarm, SLUG, forge, URL, AUTHOR, ["f1"], "")
    assert _states(again) == [("f1", "read", "", (AUTHOR, SECOND))]
    assert record.read(swarm, SLUG, URL, HEAD) == again


@pytest.mark.feature("review-findings-read")
def test_reads_add_no_posts() -> None:
    swarm, forge = _posted(["First", "Second"])
    assert swarm.redis is not None
    counter = swarm.key(SLUG, "review-tasks")
    before = swarm.redis.hgetall(counter)
    assert before
    record.mark(swarm, SLUG, forge, URL, AUTHOR, [], "")
    record.mark(swarm, SLUG, forge, URL, AUTHOR, ["f2"], "Answered")
    assert swarm.redis.hgetall(counter) == before
    assert forge.posts == [(7, "First"), (7, "Second")]
