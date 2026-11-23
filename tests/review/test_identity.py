from dataclasses import replace

import pytest

from scripts.forge.model import ChangeRequest
from scripts.review import record
from scripts.swarm.store import SwarmError
from tests.review.fakes import AUTHOR, HEAD, REVIEWER, SLUG, URL, FakeForge, store

pytestmark = pytest.mark.unit


@pytest.mark.feature("review-findings-queue-gate")
@pytest.mark.parametrize("action", ["post", "read"])
def test_a_forge_for_another_request_cannot_write_the_record(monkeypatch: pytest.MonkeyPatch, action: str) -> None:
    found, forge = store(), FakeForge()
    original = record.post(found, SLUG, forge, URL, REVIEWER, "r1", ["First"])
    read = forge.read
    post = forge.post
    other = URL.replace("/pull/", "-other/pull/")

    def wrong_read(number: int) -> ChangeRequest:
        return replace(read(number), url=other)

    def wrong_post(number: int, body: str) -> str:
        return post(number, body).replace(URL, other)

    monkeypatch.setattr(forge, "read", wrong_read)
    monkeypatch.setattr(forge, "post", wrong_post)
    with pytest.raises(SwarmError, match="does not match"):
        if action == "post":
            record.post(found, SLUG, forge, URL, REVIEWER, "r1", ["Second"])
        else:
            record.mark(found, SLUG, forge, URL, AUTHOR, ["f1"], "")
    assert [body for _, body in forge.posts] == ["First"]
    assert record.read(found, SLUG, URL, HEAD) == original
