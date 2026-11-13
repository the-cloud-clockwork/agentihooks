import stat

import pytest
from dotenv import dotenv_values

from scripts.events import nkey
from scripts.events.config import BusConfig
from scripts.hive import auth, cli, daemon, nats_grant, registry

pytestmark = pytest.mark.unit

URL = "nats://hub.example:4222"


def _signer(member_swarms: dict | None = None) -> nats_grant.Signer:
    operator, account = nkey.create(nkey.OPERATOR), nkey.create(nkey.ACCOUNT)
    return nats_grant.Signer(operator, account, "system", URL, operator.public, member_swarms)


@pytest.mark.feature("hive-root-bus-settings")
def test_the_root_bus_env_resolves_to_nats_with_a_root_member_credential(tmp_path):
    from scripts.hive import root_bus

    signer = _signer()
    path = root_bus.write(tmp_path, signer, "root")

    assert path == tmp_path / "root-bus" / "bus.env"
    values = {key: value or "" for key, value in dotenv_values(path, interpolate=False).items()}
    creds = tmp_path / "root-bus" / "bus.creds"
    assert values == {
        "AGENTIHOOKS_BUS": "nats",
        "AGENTIHOOKS_BUS_URL": URL,
        "AGENTIHOOKS_BUS_CREDENTIALS": str(creds),
        "AGENTIHOOKS_BUS_BIND": "1",
    }
    config = BusConfig.from_environ(values)
    assert (config.backend, config.url, config.provision) == ("nats", URL, False)
    jwt, user = nkey.parse_creds(config.credentials or "")
    claims = nkey.claims(jwt)
    assert claims["name"] == "hive-root"
    assert claims["iss"] == signer.account.public
    assert claims["sub"] == user.public == nats_grant.member_user(signer, "root").public
    assert "scheduler-root" in " ".join(claims["nats"]["pub"]["allow"])
    assert stat.S_IMODE(creds.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.feature("hive-root-bus-settings")
def test_the_root_bus_settings_stay_out_of_the_env_files_every_process_loads(tmp_path):
    from scripts import operator_env
    from scripts.hive import root_bus

    (tmp_path / ".env").write_text("")
    root_bus.write(tmp_path, _signer(), "root")

    environ = {"HOME": str(tmp_path / "home"), "AGENTIHOOKS_HOME": str(tmp_path)}
    assert operator_env.files(environ) == [tmp_path / ".env"]


@pytest.mark.feature("hive-root-bus-settings")
def test_a_signer_that_lists_member_swarms_without_root_mints_nothing(tmp_path):
    from scripts.hive import root_bus

    with pytest.raises(auth.HiveError, match="root"):
        root_bus.write(tmp_path, _signer({"other": ("alpha",)}), "root")
    assert not (tmp_path / "root-bus").exists()


@pytest.mark.feature("hive-root-bus-settings")
def test_the_hive_service_loads_the_root_bus_env_after_the_hive_env():
    text = daemon.unit("/bin/agentihooks")

    hive = text.index("EnvironmentFile=-%h/.agentihooks/hive.env\n")
    root = text.index("EnvironmentFile=-%h/.agentihooks/root-bus/bus.env\n")
    assert hive < root < text.index("ExecStart=")


@pytest.mark.feature("hive-root-bus-settings")
def test_hive_root_bus_writes_the_settings_then_reinstalls_and_restarts_the_service(tmp_path, monkeypatch, capsys):
    from scripts.swarm import commands

    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    monkeypatch.delenv("AGENTIHOOKS_HIVE_ID", raising=False)
    monkeypatch.setattr(cli, "_signer", _signer)
    monkeypatch.setattr(commands, "hive_id", lambda: "root")
    steps: list[str] = []
    monkeypatch.setattr(daemon, "install", lambda: steps.append("install") or True)
    monkeypatch.setattr(daemon, "restart", lambda: steps.append("restart") or True)

    assert cli.main(["root-bus"]) == 0

    assert steps == ["install", "restart"]
    path = registry.home() / "root-bus" / "bus.env"
    assert dotenv_values(path)["AGENTIHOOKS_BUS"] == "nats"
    out = capsys.readouterr().out
    assert out == f"wrote the NATS bus settings of hive root to {path} and restarted the hive service\n"
    assert "SEED" not in out


@pytest.mark.feature("hive-root-bus-settings")
def test_hive_root_bus_refuses_without_the_hub_signer(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    monkeypatch.delenv("AGENTIHOOKS_HIVE_ID", raising=False)
    monkeypatch.setattr(cli, "_signer", lambda: None)

    assert cli.main(["root-bus"]) == 1

    assert capsys.readouterr().err == (
        "hive root-bus refused: AGENTIHOOKS_NATS_SIGNER must name the hub NATS signer to mint the root hive credential\n"
    )
    assert not (tmp_path / "root-bus").exists()


@pytest.mark.feature("hive-root-bus-settings")
def test_hive_root_bus_refuses_on_a_joined_member_host(tmp_path, monkeypatch, capsys):
    from scripts.hive import scope

    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    monkeypatch.setattr(scope, "member", lambda: "hive-7")
    monkeypatch.setattr(cli, "_signer", _signer)

    assert cli.main(["root-bus"]) == 1

    assert capsys.readouterr().err == (
        "hive root-bus refused: this host joined as hive member hive-7, whose hive env carries its bus settings\n"
    )
    assert not (tmp_path / "root-bus").exists()


def test_restart_restarts_the_hive_user_service():
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return type("Done", (), {"returncode": 0})()

    assert daemon.restart(run) is True
    assert calls == [
        (["systemctl", "--user", "restart", daemon.UNIT], {"capture_output": True, "text": True, "timeout": 30})
    ]
