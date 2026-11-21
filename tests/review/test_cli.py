"""The review agent commands: a reviewer posts findings and the author marks them, as the calling swarm agent."""

import json

import pytest

from scripts import cli_delegates
from scripts.review import cli, record
from scripts.swarm.store import RedisStore
from tests.review.fakes import AUTHOR, FEATURE, HEAD, REVIEWER, SLUG, URL, FakeForge, swarm

pytestmark = pytest.mark.unit


def _wired(monkeypatch: pytest.MonkeyPatch, name: str) -> tuple[RedisStore, FakeForge]:
    found, forge = swarm({}, name=name, task="r1"), FakeForge()
    monkeypatch.setattr(cli, "connect", lambda: found)
    monkeypatch.setattr(cli, "_forge", lambda: forge)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", name)
    return found, forge


@pytest.mark.feature(FEATURE)
def test_the_command_line_delegates_review_to_this_module() -> None:
    assert cli_delegates.DELEGATED_CLIS["review"] == "scripts.review.cli"
    assert cli_delegates.delegated_cli(["review", "post"]) is cli.main


@pytest.mark.feature(FEATURE)
def test_post_records_the_calling_reviewer_and_prints_the_record(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    found, forge = _wired(monkeypatch, REVIEWER)
    assert cli.main(["post", URL, "--slug", SLUG, "--finding", "First", "--finding", "Second"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["head"] == HEAD
    assert [(item["id"], item["by"], item["task"]) for item in printed["findings"]] == [
        ("f1", REVIEWER, "r1"),
        ("f2", REVIEWER, "r1"),
    ]
    assert forge.posts == [(7, "First"), (7, "Second")]
    assert record.done_refusal(found, SLUG, "review", "r1") is None


@pytest.mark.feature(FEATURE)
def test_read_marks_the_findings_answered_by_the_calling_author(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    found, forge = _wired(monkeypatch, AUTHOR)
    record.post(found, SLUG, forge, URL, REVIEWER, "r2", ["First"])
    assert cli.main(["read", URL, "--slug", SLUG, "--finding", "f1", "--answer", "Fixed"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert [(item["state"], item["answer"]) for item in printed["findings"]] == [("answered", "Fixed")]


@pytest.mark.feature(FEATURE)
def test_a_refusal_prints_its_reason_and_returns_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _wired(monkeypatch, REVIEWER)
    assert cli.main(["post", URL, "--slug", SLUG]) == 1
    assert capsys.readouterr() == ("", "review: a review posts at least one finding\n")


@pytest.mark.feature(FEATURE)
def test_a_session_outside_the_swarm_is_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _wired(monkeypatch, REVIEWER)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "stranger@x")
    assert cli.main(["post", URL, "--slug", SLUG, "--finding", "First"]) == 1
    assert capsys.readouterr().err == "review: stranger@x is not an agent of swarm sw\n"
