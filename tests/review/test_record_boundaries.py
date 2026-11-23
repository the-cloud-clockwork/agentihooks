"""Review record keys: isolation by swarm and change request, the stored layout and change request URL parsing."""

import json
from hashlib import sha256

import pytest

from scripts.review import record
from scripts.swarm.store import SwarmError
from tests.review.fakes import HEAD, REVIEWER, SLUG, URL, FakeForge, store

pytestmark = pytest.mark.unit


@pytest.mark.feature("review-record-post")
def test_findings_are_isolated_by_swarm_and_change_request() -> None:
    found = store()
    other_url = URL.replace("/pull/7", "/pull/8")
    record.post(found, SLUG, FakeForge(), URL, REVIEWER, "r1", ["First"])
    record.post(found, "other", FakeForge(), URL, REVIEWER, "r2", ["Second"])
    record.post(found, SLUG, FakeForge(url=other_url), other_url, REVIEWER, "r3", ["Third"])
    for slug, url, body in [(SLUG, URL, "First"), ("other", URL, "Second"), (SLUG, other_url, "Third")]:
        saved = record.read(found, slug, url, HEAD)
        assert saved is not None
        assert (saved.url, saved.head, [item.body for item in saved.findings]) == (url, HEAD, [body])
    assert record.read(found, "third", URL, HEAD) is None


@pytest.mark.feature("review-record-post")
def test_a_saved_record_reads_back_from_its_hash_field() -> None:
    found = store()
    saved = record.Record(URL, HEAD, (record.Finding("f1", "First", f"{URL}#c1", REVIEWER, "r1"),))
    key = found.key(SLUG, "review", sha256(URL.encode()).hexdigest()[:16])
    found.redis.hset(key, HEAD, json.dumps(record.document(saved)))
    assert record.read(found, SLUG, URL, HEAD) == saved
    assert record.document(saved) == {
        "url": URL,
        "head": HEAD,
        "findings": [{"id": "f1", "body": "First", "comment": f"{URL}#c1", "by": REVIEWER, "task": "r1"}],
    }


@pytest.mark.feature("review-record-post")
@pytest.mark.parametrize(("requested", "reported"), [(URL + "/", URL), (URL, URL + "/")])
def test_trailing_slashes_do_not_change_the_forge_target(requested: str, reported: str) -> None:
    forge = FakeForge(url=reported)
    saved = record.post(store(), SLUG, forge, requested, REVIEWER, "r1", ["First"])
    assert (saved.url, saved.head, forge.reads, forge.posts) == (requested, HEAD, [7], [(7, "First")])


@pytest.mark.feature("review-record-post")
def test_a_forge_url_with_a_suffix_is_a_different_request() -> None:
    forge = FakeForge(url=URL + "X")
    with pytest.raises(SwarmError) as error:
        record.post(store(), SLUG, forge, URL, REVIEWER, "r1", ["First"])
    assert str(error.value) == f"change request {URL} does not match forge target {URL}X"
    assert forge.posts == []


@pytest.mark.feature("review-record-post")
def test_the_change_request_number_is_the_last_path_part() -> None:
    assert record.number_of("7") == 7
    assert record.number_of(URL + "/") == 7
    assert record.number_of("https://gitlab.com/g/r/-/merge_requests/12?tab=diffs") == 12
    with pytest.raises(SwarmError) as error:
        record.number_of(URL + "X")
    assert str(error.value) == f"not a change request URL: {URL}X"
