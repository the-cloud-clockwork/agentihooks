"""The review read command: the author marks findings read or answered as the calling swarm agent."""

import json

import pytest

from scripts.gates.base import Who
from scripts.review import cli, record
from scripts.swarm.store import RedisStore
from tests.review.fakes import HEAD, REVIEWER, SLUG, URL, FakeForge, swarm

pytestmark = pytest.mark.unit

AUTHOR = "engineer@a1b2c3-0001"


def _wired(monkeypatch: pytest.MonkeyPatch, name: str = AUTHOR) -> RedisStore:
    found, forge = swarm(name=name, task="t1"), FakeForge()
    monkeypatch.setattr(cli, "connect", lambda: found)
    monkeypatch.setattr(cli, "_forge", lambda: forge)
    monkeypatch.setattr(cli.Who, "from_env", lambda: Who(name=name))
    record.post(found, SLUG, forge, URL, REVIEWER, "r1", ["First", "Second"])
    return found


def _stored(found: RedisStore) -> record.Record:
    saved = record.read(found, SLUG, URL, HEAD)
    assert saved is not None
    return saved


@pytest.mark.feature("review-findings-read")
def test_read_marks_the_chosen_finding_answered_by_the_calling_author(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    found = _wired(monkeypatch)
    assert cli.main(["read", URL, "--slug", SLUG, "--finding", "f2", "--answer", "Fixed"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert (printed["url"], printed["head"]) == (URL, HEAD)
    assert [(item["id"], item["state"], item["answer"], item["read_by"]) for item in printed["findings"]] == [
        ("f1", "open", "", []),
        ("f2", "answered", "Fixed", [AUTHOR]),
    ]
    assert printed == record.document(_stored(found))


@pytest.mark.feature("review-findings-read")
def test_read_without_options_marks_every_finding_read(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _wired(monkeypatch)
    assert cli.main(["read", URL, "--slug", SLUG]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert [(item["state"], item["answer"], item["read_by"]) for item in printed["findings"]] == [
        ("read", "", [AUTHOR]),
        ("read", "", [AUTHOR]),
    ]


@pytest.mark.feature("review-findings-read")
def test_a_refused_read_prints_its_reason_and_returns_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    found = _wired(monkeypatch, REVIEWER)
    assert cli.main(["read", URL, "--slug", SLUG, "--answer", "Mine"]) == 1
    assert capsys.readouterr() == ("", "review: a reviewer cannot mark its own findings read: f1, f2\n")
    assert [(item.state, item.answer, item.read_by) for item in _stored(found).findings] == [
        ("open", "", ()),
        ("open", "", ()),
    ]
