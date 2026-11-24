"""A review record is written only through a forge that reports the requested change request."""

import pytest

from scripts.review import record
from scripts.swarm.store import SwarmError
from tests.review.fakes import HEAD, REVIEWER, SLUG, URL, FakeForge, store

pytestmark = pytest.mark.unit


@pytest.mark.feature("review-record-post")
def test_a_forge_for_another_request_cannot_write_the_record() -> None:
    found = store()
    original = record.post(found, SLUG, FakeForge(), URL, REVIEWER, "r1", ["First"])
    other = URL.replace("/pull/", "-other/pull/")
    forge = FakeForge(url=other)
    with pytest.raises(SwarmError) as error:
        record.post(found, SLUG, forge, URL, REVIEWER, "r1", ["Second"])
    assert str(error.value) == f"change request {URL} does not match forge target {other}"
    assert (forge.reads, forge.posts) == ([7], [])
    assert record.read(found, SLUG, URL, HEAD) == original
    assert found.redis.hgetall(found.key(SLUG, "review-tasks")) == {"r1": "1"}
