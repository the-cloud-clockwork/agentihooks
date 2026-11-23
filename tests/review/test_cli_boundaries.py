import json
from unittest.mock import Mock

import pytest

from scripts.gates.base import Who
from scripts.review import cli, record
from scripts.swarm.store import SwarmError
from tests.review.fakes import AUTHOR, REVIEWER, SLUG, URL, FakeForge, swarm
from tests.review.test_cli import _wired

pytestmark = pytest.mark.unit


@pytest.mark.feature("review-findings-queue-gate")
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


@pytest.mark.feature("review-findings-queue-gate")
@pytest.mark.parametrize("args", [[], ["post", URL], ["read", URL]])
def test_review_requires_an_action_and_swarm(args: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        cli.parser().parse_args(args)
    assert error.value.code == 2
    message = capsys.readouterr().err
    assert message.startswith("usage: agentihooks review ")
    assert ("--slug" if args else "action") in message


@pytest.mark.feature("review-findings-queue-gate")
def test_help_explains_posting_and_author_read_commands() -> None:
    help_text = " ".join(cli.parser().format_help().split())
    assert help_text.startswith("usage: agentihooks review ")
    assert "post findings on the change request head through the forge" in help_text
    assert "mark findings on the current head read, or answered with --answer" in help_text


@pytest.mark.feature("review-findings-queue-gate")
def test_read_without_an_answer_retains_the_calling_author(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    found, forge = _wired(monkeypatch, AUTHOR)
    record.post(found, SLUG, forge, URL, REVIEWER, "r2", ["First"])
    assert cli.main(["read", URL, "--slug", SLUG]) == 0
    finding = json.loads(capsys.readouterr().out)["findings"][0]
    assert (finding["state"], finding["answer"], finding["read_by"]) == ("read", "", [AUTHOR])


@pytest.mark.feature("review-findings-queue-gate")
def test_agent_lookup_uses_the_requested_swarm(monkeypatch: pytest.MonkeyPatch) -> None:
    found = swarm({}, name=REVIEWER)
    agents = Mock(return_value=found.agents(SLUG))
    monkeypatch.setattr(found, "agents", agents)
    monkeypatch.setattr(cli.Who, "from_env", lambda: Who(name=REVIEWER))
    assert cli._agent(found, SLUG).name == REVIEWER
    agents.assert_called_once_with(SLUG)


@pytest.mark.feature("review-findings-queue-gate")
def test_an_unnamed_session_receives_the_membership_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.Who, "from_env", lambda: Who())
    with pytest.raises(SwarmError) as error:
        cli._agent(swarm({}), SLUG)
    assert str(error.value) == "this session is not an agent of swarm sw"
