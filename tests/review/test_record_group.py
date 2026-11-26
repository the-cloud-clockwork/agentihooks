"""Review post on a merge queue group commit link posts on each pull request the group tests."""

import json
from importlib import import_module

import pytest

from scripts.forge.model import ChangeRequest
from scripts.gates.base import Who
from scripts.review import cli, record
from scripts.swarm.store import RedisStore, SwarmError
from tests.review.fakes import HEAD, REVIEWER, SLUG, URL, FakeForge, store, swarm

pytestmark = pytest.mark.unit
REPO = "https://github.com/o/r"
GROUP = "0f1e2d3c4b5a69788796a5b4c3d2e1f00f1e2d3c"
BASE = "aaaabbbbccccddddeeeeffff0000111122223333"
LINK = f"{REPO}/commit/{GROUP}"
AUTHOR = Who(name="engineer@a1b2c3-0001", swarm=SLUG, task="t1")


class GroupForge:
    """A forge whose merge group names ``pulls`` and whose pull requests ``foreign`` read under another repository."""

    def __init__(self, pulls: tuple[int, ...] = (7, 8), foreign: tuple[int, ...] = ()) -> None:
        self.pulls = pulls
        self.foreign = foreign
        self.compared: list[tuple[str, str]] = []
        self.posts: list[tuple[int, str]] = []

    def group_pulls(self, base: str, head: str) -> tuple[int, ...]:
        self.compared.append((base, head))
        return self.pulls

    def read(self, number: int) -> ChangeRequest:
        repository = "https://github.com/x/y" if number in self.foreign else REPO
        return ChangeRequest(number, f"{repository}/pull/{number}", "Title", "Body", f"head{number}", "", (), ())

    def post(self, number: int, body: str) -> str:
        self.posts.append((number, body))
        return f"{REPO}/pull/{number}#issuecomment-{len(self.posts)}"


def _grouped(number: int = 8) -> RedisStore:
    found = store()
    record.remember_group(found, SLUG, record.Group(REPO, GROUP, BASE, number))
    return found


def _findings(found: record.Record) -> list[tuple[str, str, str, str, str]]:
    return [(item.id, item.body, item.comment, item.by, item.task) for item in found.findings]


@pytest.mark.feature("review-queue-group-post")
def test_a_pull_link_post_records_only_at_the_pull_head() -> None:
    found, forge = store(), FakeForge()
    posted = record.post(found, SLUG, forge, URL, REVIEWER, "r1", ["First"])
    assert forge.posts == [(7, "First")]
    assert (posted.url, posted.head) == (URL, HEAD)
    assert record.read(found, SLUG, URL, HEAD) == posted
    assert record.read(found, SLUG, URL, GROUP) is None


@pytest.mark.feature("review-queue-group-post")
def test_a_pull_link_post_still_refuses_a_commit_link_and_posts_nothing() -> None:
    found, forge = store(), FakeForge()
    with pytest.raises(SwarmError, match=f"^not a change request URL: {LINK}$"):
        record.post(found, SLUG, forge, LINK, REVIEWER, "r1", ["First"])
    assert (forge.reads, forge.posts) == ([], [])


@pytest.mark.feature("review-queue-group-post")
def test_the_queue_gate_reads_no_review_for_a_head_whose_findings_sit_under_another_head() -> None:
    gate = import_module("scripts.review.gate")
    found = store()
    record.post(found, SLUG, FakeForge(head=GROUP), URL, REVIEWER, "r1", ["First"])
    own = gate.refusal(record.read(found, SLUG, URL, HEAD), SLUG, URL, HEAD, AUTHOR)
    assert own.startswith(f"no independent review on head {HEAD[:12]} of {URL}")
    assert "are unread: f1" in gate.refusal(record.read(found, SLUG, URL, GROUP), SLUG, URL, GROUP, AUTHOR)


@pytest.mark.feature("review-queue-group-post")
def test_a_group_commit_link_posts_each_finding_on_every_pull_request_of_the_group() -> None:
    found, forge = _grouped(), GroupForge()
    posted = record.post_group(found, SLUG, forge, LINK, REVIEWER, "r1", ["First", " Second "])
    assert forge.compared == [(BASE, GROUP)]
    assert forge.posts == [(7, "First"), (7, "Second"), (8, "First"), (8, "Second")]
    assert [(item.url, item.head) for item in posted] == [(f"{REPO}/pull/7", GROUP), (f"{REPO}/pull/8", GROUP)]
    assert [_findings(item) for item in posted] == [
        [
            ("f1", "First", f"{REPO}/pull/7#issuecomment-1", REVIEWER, "r1"),
            ("f2", "Second", f"{REPO}/pull/7#issuecomment-2", REVIEWER, "r1"),
        ],
        [
            ("f1", "First", f"{REPO}/pull/8#issuecomment-3", REVIEWER, "r1"),
            ("f2", "Second", f"{REPO}/pull/8#issuecomment-4", REVIEWER, "r1"),
        ],
    ]
    assert tuple(record.read(found, SLUG, item.url, GROUP) for item in posted) == posted


@pytest.mark.feature("review-queue-group-post")
def test_each_finding_of_a_group_post_counts_once_for_its_task() -> None:
    found = _grouped()
    record.post_group(found, SLUG, GroupForge(), LINK, REVIEWER, "r1", ["First", "Second"])
    assert found.redis.hgetall(found.key(SLUG, "review-tasks")) == {"r1": "2"}


@pytest.mark.feature("review-queue-group-post")
def test_the_done_check_passes_a_review_task_after_its_group_post() -> None:
    found = _grouped()
    assert record.done_refusal(found, SLUG, "review", "review", "r1") is not None
    record.post_group(found, SLUG, GroupForge(), LINK, REVIEWER, "r1", ["First"])
    assert record.done_refusal(found, SLUG, "review", "review", "r1") is None


@pytest.mark.feature("review-queue-group-post")
def test_a_group_commit_link_without_a_group_record_is_refused_and_posts_nothing() -> None:
    found, forge = store(), GroupForge()
    with pytest.raises(SwarmError, match=f"^no merge queue group {GROUP} is recorded in swarm {SLUG}$"):
        record.post_group(found, SLUG, forge, LINK, REVIEWER, "r1", ["First"])
    assert (forge.compared, forge.posts) == ([], [])
    assert found.redis.keys("*") == []


@pytest.mark.feature("review-queue-group-post")
def test_a_group_post_without_a_finding_is_refused_and_posts_nothing() -> None:
    found, forge = _grouped(), GroupForge()
    with pytest.raises(SwarmError, match="^a review posts at least one finding$"):
        record.post_group(found, SLUG, forge, LINK, REVIEWER, "r1", ["", "  "])
    assert (forge.compared, forge.posts) == ([], [])


@pytest.mark.feature("review-queue-group-post")
def test_a_group_whose_commit_titles_name_no_pull_request_posts_on_its_queue_ref_pull() -> None:
    found, forge = _grouped(number=8), GroupForge(pulls=())
    posted = record.post_group(found, SLUG, forge, LINK, REVIEWER, "r1", ["First"])
    assert forge.posts == [(8, "First")]
    assert [(item.url, item.head) for item in posted] == [(f"{REPO}/pull/8", GROUP)]


@pytest.mark.feature("review-queue-group-post")
def test_a_group_naming_no_pull_request_is_refused_and_posts_nothing() -> None:
    found, forge = _grouped(number=0), GroupForge(pulls=())
    with pytest.raises(SwarmError, match=f"^merge queue group {GROUP} names no pull request$"):
        record.post_group(found, SLUG, forge, LINK, REVIEWER, "r1", ["First"])
    assert forge.posts == []


@pytest.mark.feature("review-queue-group-post")
def test_a_group_pull_request_the_forge_reads_elsewhere_stops_every_post() -> None:
    forge = GroupForge(foreign=(8,))
    with pytest.raises(SwarmError, match=f"^change request {REPO}/pull/8 does not match forge target "):
        record.post_group(_grouped(), SLUG, forge, LINK, REVIEWER, "r1", ["First"])
    assert forge.posts == []


@pytest.mark.feature("review-queue-group-post")
def test_a_group_record_holds_its_change_under_the_review_keys_and_expires_with_them() -> None:
    found = _grouped()
    key = found.key(SLUG, "review", "group", GROUP)
    assert found.redis.hgetall(key) == {"repository": REPO, "head": GROUP, "base": BASE, "number": "8"}
    assert record.TTL_SEC - 5 <= found.redis.ttl(key) <= record.TTL_SEC
    assert record.group_of(found, SLUG, GROUP) == record.Group(REPO, GROUP, BASE, 8)
    assert (record.group_of(found, SLUG, BASE), record.group_of(found, "other", GROUP)) == (None, None)


@pytest.mark.feature("review-queue-group-post")
def test_the_review_command_prints_one_document_per_pull_request_of_a_group(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    found = swarm(task="r1")
    record.remember_group(found, SLUG, record.Group(REPO, GROUP, BASE, 8))
    monkeypatch.setattr(cli, "connect", lambda: found)
    monkeypatch.setattr(cli, "_forge", GroupForge)
    monkeypatch.setattr(cli.Who, "from_env", lambda: Who(name=REVIEWER))
    assert cli.main(["post", LINK, "--slug", SLUG, "--finding", "First"]) == 0
    pulls = (f"{REPO}/pull/7", f"{REPO}/pull/8")
    saved = [record.document(item) for pull in pulls if (item := record.read(found, SLUG, pull, GROUP))]
    assert (len(saved), json.loads(capsys.readouterr().out)) == (2, saved)
