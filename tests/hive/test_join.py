import os
from pathlib import Path

import pytest
from dotenv import dotenv_values

from scripts.hive import auth, cli


@pytest.fixture
def invitation(tmp_path, monkeypatch):
    home = tmp_path / "joined"
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(home))
    monkeypatch.setenv("AGENTIHOOKS_HIVE_URL", "https://previous-hub.example.test")
    invite = tmp_path / "invite"
    invite.write_text(
        auth.env_text(
            "https://hub.example.test",
            {
                "id": "fixture-member",
                "ledger_credential": "fixture-invite",
                "redis_url": "redis://retired.example.test/0",
            },
        )
    )
    with invite.open("a") as stream:
        stream.write(
            "AGENTIHOOKS_BUS=nats\nAGENTIHOOKS_BUS_URL=nats://bus.example.test:4222\nAGENTIHOOKS_SWARM_URL=https://ledger.example.test\n"
        )
    return invite, home


def test_one_invite_joins_a_machine_and_saves_the_refreshed_nats_grant(invitation, monkeypatch, capsys):
    invite, home = invitation
    calls = []

    def refresh(url, path, body, action, environ):
        calls.append((url, path, body, action))
        return {
            "id": "fixture-member",
            "redis_url": "redis://refreshed.example.test/0",
            "nats_credentials": "fixture nats credentials\n",
        }

    monkeypatch.setattr(cli, "_post", refresh)
    assert cli.main(["join", str(invite)]) == 0
    assert calls == [("https://hub.example.test", "/hive/refresh", {"ledger_credential": "fixture-invite"}, "join")]
    saved = dotenv_values(home / auth.ENV_FILE)
    assert saved["AGENTIHOOKS_BUS"] == "nats"
    assert saved["AGENTIHOOKS_BUS_URL"] == "nats://bus.example.test:4222"
    assert saved["AGENTIHOOKS_SWARM_URL"] == "https://ledger.example.test"
    assert saved["AGENTIHOOKS_HIVE_ID"] == "fixture-member"
    assert saved["AGENTIHOOKS_HIVE_URL"] == "https://hub.example.test"
    assert saved["AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL"] == "fixture-invite"
    assert saved["AGENTIHOOKS_HIVE_REDIS_URL"] == "redis://refreshed.example.test/0"
    creds = Path(saved["AGENTIHOOKS_BUS_CREDENTIALS"])
    assert creds.read_text() == "fixture nats credentials\n"
    assert creds.stat().st_mode & 0o777 == 0o600
    assert (home / auth.ENV_FILE).stat().st_mode & 0o777 == 0o600
    output = capsys.readouterr()
    assert "fixture-invite" not in output.out + output.err
    assert "fixture nats credentials" not in output.out + output.err


def test_a_refused_invite_keeps_the_current_membership(invitation, monkeypatch, capsys):
    invite, home = invitation
    home.mkdir()
    saved = home / auth.ENV_FILE
    saved.write_text("previous membership\n")

    def refused(*args):
        raise auth.HiveError("invite refused")

    monkeypatch.setattr(cli, "_post", refused)
    assert cli.main(["join", str(invite)]) == 1
    assert saved.read_text() == "previous membership\n"
    assert "invite refused" in capsys.readouterr().err


@pytest.mark.parametrize("content", ["", "AGENTIHOOKS_HIVE_URL=https://hub.example.test\n"])
def test_an_incomplete_invite_is_refused_before_contacting_the_hub(invitation, monkeypatch, content, capsys):
    invite, home = invitation
    invite.write_text(content)
    monkeypatch.setattr(cli, "_post", lambda *args: pytest.fail("invalid invite reached hub"))
    assert cli.main(["join", str(invite)]) == 1
    assert capsys.readouterr().err == "hive join refused: the invite needs a hive id, endpoint and ledger credential\n"
    assert not (home / auth.ENV_FILE).exists()


def test_an_unreadable_invite_is_reported_without_joining(invitation, capsys):
    invite, home = invitation
    assert cli.main(["join", str(invite / "missing")]) == 1
    assert "the invite file could not be read" in capsys.readouterr().err
    assert not (home / auth.ENV_FILE).exists()


def test_a_grant_for_another_member_does_not_replace_membership(invitation, monkeypatch, capsys):
    invite, home = invitation
    home.mkdir()
    saved = home / auth.ENV_FILE
    saved.write_text("previous membership\n")
    monkeypatch.setattr(cli, "_post", lambda *args: {"id": "another-member"})
    assert cli.main(["join", str(invite)]) == 1
    assert "different invited member" in capsys.readouterr().err
    assert saved.read_text() == "previous membership\n"


def test_multiline_invite_settings_are_refused_before_refresh(invitation, monkeypatch, capsys):
    invite, home = invitation
    with invite.open("a") as stream:
        stream.write('AGENTIHOOKS_BUS_URL="nats://bus.example.test\\nINJECTED=value"\n')
    monkeypatch.setattr(cli, "_post", lambda *args: pytest.fail("invalid invite reached hub"))
    assert cli.main(["join", str(invite)]) == 1
    assert capsys.readouterr().err == "hive join refused: invite settings must each fit on one line\n"
    assert not (home / auth.ENV_FILE).exists()


def test_an_invite_cannot_send_its_credential_over_remote_plain_http(invitation, capsys):
    invite, home = invitation
    invite.write_text(invite.read_text().replace("https://hub.example.test", "http://hub.example.test"))
    assert cli.main(["join", str(invite)]) == 1
    assert "must be https" in capsys.readouterr().err
    assert not (home / auth.ENV_FILE).exists()


def test_connection_settings_survive_a_later_refresh(invitation, monkeypatch):
    invite, home = invitation
    monkeypatch.delenv("AGENTIHOOKS_HIVE_URL")
    monkeypatch.delenv("AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL", raising=False)
    monkeypatch.setattr(
        cli,
        "_post",
        lambda *args: {
            "id": "fixture-member",
            "redis_url": "redis://fresh.example.test/0",
        },
    )
    assert cli.main(["join", str(invite)]) == 0
    assert cli.main(["refresh"]) == 0
    saved = dotenv_values(home / auth.ENV_FILE)
    assert saved["AGENTIHOOKS_BUS"] == "nats"
    assert saved["AGENTIHOOKS_BUS_URL"] == "nats://bus.example.test:4222"
    assert saved["AGENTIHOOKS_SWARM_URL"] == "https://ledger.example.test"


def test_an_older_invite_without_bus_settings_still_joins(invitation, monkeypatch):
    invite, home = invitation
    invite.write_text(
        "\n".join(line for line in invite.read_text().splitlines() if line.startswith("AGENTIHOOKS_HIVE_"))
    )
    monkeypatch.setattr(
        cli, "_post", lambda *args: {"id": "fixture-member", "redis_url": "redis://fresh.example.test/0"}
    )
    assert cli.main(["join", str(invite)]) == 0
    assert "AGENTIHOOKS_BUS" not in dotenv_values(home / auth.ENV_FILE)


def test_join_help_names_the_single_invite_file(capsys):
    with pytest.raises(SystemExit) as exited:
        cli.main(["join", "--help"])
    assert exited.value.code == 0
    assert "private member invite file shared with Kubernetes workers" in capsys.readouterr().out
    command = next(action for action in cli._parser()._actions if action.dest == "command")
    assert next(choice.help for choice in command._choices_actions if choice.dest == "join") == (
        "Join this host with one invite file from hive invite"
    )


def test_join_keeps_invite_values_literal(invitation, monkeypatch):
    invite, home = invitation
    invite.write_text(invite.read_text().replace("fixture-invite", "${INVITE_REFERENCE}"))
    monkeypatch.setenv("INVITE_REFERENCE", "a different credential")
    seen = []

    def refresh(url, path, body, action, environ):
        seen.append(body)
        return {"id": "fixture-member", "redis_url": "redis://fresh.example.test/0"}

    monkeypatch.setattr(cli, "_post", refresh)
    assert cli.main(["join", str(invite)]) == 0
    assert seen == [{"ledger_credential": "${INVITE_REFERENCE}"}]
    assert (
        dotenv_values(home / auth.ENV_FILE, interpolate=False)["AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL"]
        == "${INVITE_REFERENCE}"
    )


def test_join_places_the_ca_delivered_by_the_hub(invitation, monkeypatch):
    invite, home = invitation
    monkeypatch.setattr(
        cli,
        "_post",
        lambda *args: {
            "id": "fixture-member",
            "redis_url": "redis://fresh.example.test/0",
            "ledger_ca": "delivered CA",
        },
    )
    placed = []
    monkeypatch.setattr(cli.ledger_ca, "place", lambda *args: placed.append(args))
    assert cli.main(["join", str(invite)]) == 0
    assert len(placed) == 1
    assert placed[0][:2] == (home, "delivered CA")
    assert placed[0][2] is os.environ
