"""Members minted before the auth service get a holder record by backfill, and agent token issue reads only holder records."""

import argparse
from typing import Any
from unittest.mock import patch

import pytest

from scripts.hive import auth, cli

pytestmark = pytest.mark.unit

SLUG = "alpha"
WORKER = "engineer@323133-7776"
PUBLIC = "redis://hive.example:6380/2"


@pytest.fixture
def client() -> Any:
    import fakeredis

    return fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)


def _legacy(client: Any, name: str) -> dict[str, Any]:
    """A member minted the way members were before the auth service: no holder record."""
    grant = auth.mint(client, name, PUBLIC)
    auth.hive_state.amend(auth._store(client), f"holder.{grant['id']}", lambda _: None)
    return grant


def _holder(client: Any, member_id: str) -> object:
    return auth.hive_state.read(auth._store(client), f"holder.{member_id}")


@pytest.mark.feature("hive-agent-token-holder-only")
def test_issue_for_a_legacy_member_reads_no_member_map_with_legacy_credentials_on(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    grant = _legacy(client, "laptop")
    monkeypatch.setenv("AGENTIHOOKS_AUTH_LEGACY", "on")
    monkeypatch.setenv("AGENTIHOOKS_HIVE_HOLDERS", "enforce")

    with (
        patch.object(auth, "member", side_effect=AssertionError("read the member map")),
        patch.object(auth, "_legacy_member", side_effect=AssertionError("read the legacy member map")),
        pytest.raises(auth.HiveError, match="^Hive membership was revoked$"),
    ):
        auth.issue_agent(client, grant["id"], SLUG, WORKER)
    assert _holder(client, grant["id"]) is None


@pytest.mark.feature("hive-agent-token-holder-only")
def test_until_enforced_issue_backfills_and_logs_a_legacy_member_once_even_with_legacy_credentials_off(
    client: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    grant = _legacy(client, "laptop")
    monkeypatch.setenv("AGENTIHOOKS_AUTH_LEGACY", "off")
    monkeypatch.delenv("AGENTIHOOKS_HIVE_HOLDERS", raising=False)

    first = auth.issue_agent(client, grant["id"], SLUG, WORKER)

    assert _holder(client, grant["id"]) == auth._digest(grant["ledger_credential"])
    assert capsys.readouterr().err == (
        f"hive agent token: member {grant['id']} had no holder record, wrote it from its member record; "
        "AGENTIHOOKS_HIVE_HOLDERS=enforce refuses it\n"
    )
    with patch.object(auth, "member", side_effect=AssertionError("read the member map")):
        second = auth.issue_agent(client, grant["id"], SLUG, WORKER)
    assert capsys.readouterr().err == ""
    assert {auth.agent_member(client, token, SLUG, WORKER) for token in (first, second)} == {grant["id"]}


@pytest.mark.feature("hive-agent-token-holder-only")
def test_until_enforced_an_unmapped_member_is_logged_and_refused(
    client: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    grant = _legacy(client, "laptop")
    auth.hive_state.amend(auth._store(client), f"ledger.{auth._digest(grant['ledger_credential'])}", lambda _: None)
    monkeypatch.setenv("AGENTIHOOKS_AUTH_LEGACY", "on")
    monkeypatch.delenv("AGENTIHOOKS_HIVE_HOLDERS", raising=False)

    with pytest.raises(auth.HiveError, match="^Hive membership was revoked$"):
        auth.issue_agent(client, grant["id"], SLUG, WORKER)

    assert _holder(client, grant["id"]) is None
    assert capsys.readouterr().err == (
        f"hive agent token: member {grant['id']} had no holder record, and none could be written; "
        "AGENTIHOOKS_HIVE_HOLDERS=enforce refuses it\n"
    )


@pytest.mark.feature("hive-agent-token-holder-only")
def test_the_backfill_lets_a_legacy_member_issue_without_reading_the_member_map(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    grant = _legacy(client, "laptop")
    monkeypatch.setenv("AGENTIHOOKS_AUTH_LEGACY", "on")

    assert auth.backfill_holders(client) == f"wrote the holder record of {grant['id']}"
    assert _holder(client, grant["id"]) == auth._digest(grant["ledger_credential"])
    with (
        patch.object(auth, "member", side_effect=AssertionError("read the member map")),
        patch.object(auth, "_legacy_member", side_effect=AssertionError("read the legacy member map")),
    ):
        token = auth.issue_agent(client, grant["id"], SLUG, WORKER)
    assert auth.agent_member(client, token, SLUG, WORKER) == grant["id"]


@pytest.mark.feature("hive-agent-token-holder-only")
def test_the_backfill_leaves_held_and_unmapped_members_alone(client: Any) -> None:
    held = auth.mint(client, "held", PUBLIC)
    auth.hive_state.amend(auth._store(client), f"holder.{held['id']}", lambda _: "stale")
    unmapped = _legacy(client, "unmapped")
    auth.hive_state.amend(auth._store(client), f"ledger.{auth._digest(unmapped['ledger_credential'])}", lambda _: None)

    assert auth.backfill_holders(client) == "wrote no holder record"
    assert _holder(client, held["id"]) == "stale"
    assert _holder(client, unmapped["id"]) is None


@pytest.mark.feature("hive-agent-token-holder-only")
def test_the_hive_holders_command_prints_the_backfill(client: Any, capsys: pytest.CaptureFixture[str]) -> None:
    first, second = sorted((_legacy(client, "one")["id"], _legacy(client, "two")["id"]))

    with patch.object(cli, "redis_client", return_value=client):
        assert cli.main(["holders"]) == 0

    assert capsys.readouterr().out == f"wrote the holder record of {first}, {second}\n"


@pytest.mark.feature("hive-agent-token-holder-only")
def test_the_hive_help_names_the_holders_command() -> None:
    commands = next(action for action in cli._parser()._actions if isinstance(action, argparse._SubParsersAction))
    helps = {choice.dest: choice.help for choice in commands._choices_actions}

    assert helps["holders"] == "Write the holder record of every member minted before holder records"
