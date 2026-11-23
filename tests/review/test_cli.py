"""The review post command: a swarm agent posts findings on a change request head as itself."""

import json
from unittest.mock import Mock

import pytest

from scripts import cli_delegates
from scripts.forge.model import ForgeError
from scripts.gates.base import Who
from scripts.review import cli, record
from scripts.swarm.store import RedisStore, SwarmError
from tests.review.fakes import HEAD, REVIEWER, SLUG, URL, FakeForge, swarm

pytestmark = pytest.mark.unit


def _wired(monkeypatch: pytest.MonkeyPatch, name: str = REVIEWER) -> tuple[RedisStore, FakeForge]:
    found, forge = swarm(task="r1"), FakeForge()
    monkeypatch.setattr(cli, "connect", lambda: found)
    monkeypatch.setattr(cli, "_forge", lambda: forge)
    monkeypatch.setattr(cli.Who, "from_env", lambda: Who(name=name))
    return found, forge


@pytest.mark.feature("review-record-post")
def test_the_command_line_delegates_review_to_this_module() -> None:
    assert cli_delegates.DELEGATED_CLIS["review"] == "scripts.review.cli"
    assert cli_delegates.delegated_cli(["review", "post"]) is cli.main


@pytest.mark.feature("review-record-post")
def test_post_records_the_calling_agent_and_prints_the_record(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    found, forge = _wired(monkeypatch)
    assert cli.main(["post", URL, "--slug", SLUG, "--finding", "First", "--finding", "Second"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert (printed["url"], printed["head"]) == (URL, HEAD)
    assert [(item["id"], item["body"], item["by"], item["task"]) for item in printed["findings"]] == [
        ("f1", "First", REVIEWER, "r1"),
        ("f2", "Second", REVIEWER, "r1"),
    ]
    assert forge.posts == [(7, "First"), (7, "Second")]
    saved = record.read(found, SLUG, URL, HEAD)
    assert saved is not None
    assert json.loads(json.dumps(record.document(saved))) == printed


@pytest.mark.feature("review-record-post")
def test_a_refusal_prints_its_reason_and_returns_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _, forge = _wired(monkeypatch)
    assert cli.main(["post", URL, "--slug", SLUG]) == 1
    assert capsys.readouterr() == ("", "review: a review posts at least one finding\n")
    assert forge.posts == []


@pytest.mark.feature("review-record-post")
def test_a_forge_failure_prints_its_reason_and_returns_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _, forge = _wired(monkeypatch)
    monkeypatch.setattr(forge, "read", Mock(side_effect=ForgeError("gh pr view failed")))
    assert cli.main(["post", URL, "--slug", SLUG, "--finding", "First"]) == 1
    assert capsys.readouterr() == ("", "review: gh pr view failed\n")


@pytest.mark.feature("review-record-post")
def test_a_session_outside_the_swarm_is_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    found, forge = _wired(monkeypatch, name="stranger@x")
    assert cli.main(["post", URL, "--slug", SLUG, "--finding", "First"]) == 1
    assert capsys.readouterr() == ("", "review: stranger@x is not an agent of swarm sw\n")
    assert forge.posts == []
    assert record.read(found, SLUG, URL, HEAD) is None


@pytest.mark.feature("review-record-post")
def test_an_unnamed_session_receives_the_membership_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.Who, "from_env", lambda: Who())
    with pytest.raises(SwarmError) as error:
        cli._agent(swarm(), SLUG)
    assert str(error.value) == "this session is not an agent of swarm sw"


@pytest.mark.feature("review-record-post")
def test_agent_lookup_resolves_the_name_in_the_requested_swarm(monkeypatch: pytest.MonkeyPatch) -> None:
    found = swarm()
    agents = Mock(return_value=found.agents(SLUG))
    resolve = Mock(return_value=REVIEWER)
    monkeypatch.setattr(found, "agents", agents)
    monkeypatch.setattr(found.names, "resolve", resolve)
    monkeypatch.setattr(cli.Who, "from_env", lambda: Who(name="reviewer"))
    assert cli._agent(found, SLUG).name == REVIEWER
    agents.assert_called_once_with(SLUG)
    resolve.assert_called_once_with("reviewer")


@pytest.mark.feature("review-record-post")
def test_forge_selection_uses_the_current_remote_and_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    forge = FakeForge()
    parsed = object()
    origin = Mock(return_value="git@github.com:owner/repo.git")
    parse = Mock(return_value=parsed)
    pick = Mock(return_value=forge)
    monkeypatch.setattr(cli.remote, "origin", origin)
    monkeypatch.setattr(cli.remote, "parse", parse)
    monkeypatch.setattr(cli.remote, "pick", pick)
    assert cli._forge() is forge
    origin.assert_called_once_with(".")
    parse.assert_called_once_with("git@github.com:owner/repo.git")
    pick.assert_called_once_with(parsed, None, cli.os.environ)


@pytest.mark.feature("review-record-post")
@pytest.mark.parametrize("args", [[], ["post"], ["post", URL]])
def test_review_post_requires_a_url_and_swarm(args: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        cli.parser().parse_args(args)
    assert error.value.code == 2
    message = capsys.readouterr().err
    assert message.startswith("usage: agentihooks review ")
    assert ("action" if not args else "url" if args == ["post"] else "--slug") in message


@pytest.mark.feature("review-record-post")
def test_post_help_runs_and_names_the_post_arguments(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        cli.main(["post", "--help"])
    assert error.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert help_text.startswith("usage: agentihooks review post ")
    assert "--slug SLUG" in help_text
    assert "--finding FINDING" in help_text
    assert "post findings on the change request head through the forge" in " ".join(cli.parser().format_help().split())
