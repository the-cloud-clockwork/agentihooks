"""Release review findings land on the promotion pull request that produced the release tag."""

import json
import re

import pytest

from scripts.gates.base import Who
from scripts.review import cli, record
from scripts.swarm.store import SwarmError
from tests.review.fakes import HEAD, REVIEWER, SLUG, FakeForge, store, swarm

pytestmark = pytest.mark.unit
RELEASE = "https://github.com/o/r/releases/tag/v1.2.3"
PULL = "https://github.com/o/r/pull/41"
COMMIT = "https://github.com/o/r/commit/abc123def456"
TASK = "review-rel-0123456789ab"
AUTHOR = "engineer@a1b2c3-0001"


@pytest.mark.feature("review-release-promotion-post")
def test_a_release_review_posts_every_finding_on_the_promotion_pull_request() -> None:
    found, forge = store(), FakeForge(url=PULL)
    assert record.done_refusal(found, SLUG, "review", "review", TASK) is not None
    posted = record.post(found, SLUG, forge, RELEASE, REVIEWER, TASK, ["First", "Second"])
    assert forge.tags == ["v1.2.3"]
    assert (forge.reads, forge.posts) == ([41], [(41, "First"), (41, "Second")])
    assert (posted.url, posted.head, [item.body for item in posted.findings]) == (PULL, HEAD, ["First", "Second"])
    assert record.read(found, SLUG, PULL, HEAD) == posted
    assert record.done_refusal(found, SLUG, "review", "review", TASK) is None


@pytest.mark.feature("review-release-promotion-post")
def test_the_author_marks_release_findings_on_the_promotion_pull_request() -> None:
    found, forge = store(), FakeForge(url=PULL)
    record.post(found, SLUG, forge, RELEASE, REVIEWER, TASK, ["First"])
    marked = record.mark(found, SLUG, forge, RELEASE, AUTHOR, [], "")
    assert (marked.url, [(item.state, item.read_by) for item in marked.findings]) == (PULL, [("read", (AUTHOR,))])
    assert record.read(found, SLUG, PULL, HEAD) == marked


@pytest.mark.feature("review-release-promotion-post")
def test_review_post_on_a_release_link_prints_the_promotion_pull_request_record(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    found, forge = swarm(task=TASK), FakeForge(url=PULL)
    monkeypatch.setattr(cli, "connect", lambda: found)
    monkeypatch.setattr(cli, "_forge", lambda: forge)
    monkeypatch.setattr(cli.Who, "from_env", lambda: Who(name=REVIEWER))
    assert cli.main(["post", RELEASE, "--slug", SLUG, "--finding", "First"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert (printed["url"], printed["head"], [item["body"] for item in printed["findings"]]) == (PULL, HEAD, ["First"])
    assert forge.posts == [(41, "First")]
    assert record.done_refusal(found, SLUG, "review", "review", TASK) is None


@pytest.mark.feature("review-release-promotion-post")
@pytest.mark.parametrize(
    ("link", "tag"),
    [(f"{RELEASE}/", "v1.2.3"), ("https://github.com/o/r/releases/tag/rc/releases/tag/2", "rc/releases/tag/2")],
)
def test_the_first_release_marker_splits_the_repository_from_a_tag_without_trailing_slashes(
    link: str, tag: str
) -> None:
    found, forge = store(), FakeForge(url=PULL)
    posted = record.post(found, SLUG, forge, link, REVIEWER, TASK, ["First"])
    assert (forge.tags, posted.url, forge.posts) == ([tag], PULL, [(41, "First")])


@pytest.mark.feature("review-record-post")
def test_a_merge_queue_commit_link_is_refused_and_posts_nothing() -> None:
    found, forge = store(), FakeForge()
    refused = f"^not a change request URL: {re.escape(COMMIT)}$"
    with pytest.raises(SwarmError, match=refused):
        record.number_of(COMMIT)
    with pytest.raises(SwarmError, match=refused):
        record.post(found, SLUG, forge, COMMIT, REVIEWER, "r1", ["First"])
    assert (forge.tags, forge.reads, forge.posts) == ([], [], [])
    assert found.redis.keys("*") == []
