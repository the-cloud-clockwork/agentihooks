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
def test_an_incomplete_invite_is_refused_before_contacting_the_hub(invitation, monkeypatch, content):
    invite, home = invitation
    invite.write_text(content)
    monkeypatch.setattr(cli, "_post", lambda *args: pytest.fail("invalid invite reached hub"))
    assert cli.main(["join", str(invite)]) == 1
    assert not (home / auth.ENV_FILE).exists()
