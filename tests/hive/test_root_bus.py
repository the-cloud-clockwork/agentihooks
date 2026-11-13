import os
import stat
import subprocess

import pytest
from dotenv import dotenv_values

from scripts import operator_env
from scripts.events import nkey
from scripts.events.config import BusConfig
from scripts.hive import auth, cli, daemon, nats_grant, registry

pytestmark = pytest.mark.unit

URL = "nats://hub.example:4222"


def _signer(member_swarms: dict | None = None) -> nats_grant.Signer:
    operator, account = nkey.create(nkey.OPERATOR), nkey.create(nkey.ACCOUNT)
    return nats_grant.Signer(operator, account, "system", URL, operator.public, member_swarms)


def _filled(environ: object) -> list[str]:
    assert environ is os.environ
    return []


def _from_process(value: str) -> object:
    return lambda environ: value if environ is os.environ else None


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
    assert claims["nats"] == nats_grant.member_claims(user.public, None, member="root")
    assert stat.S_IMODE((tmp_path / "root-bus").stat().st_mode) == 0o700
    assert stat.S_IMODE(creds.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.feature("hive-root-bus-settings")
def test_the_root_bus_settings_stay_out_of_the_env_files_every_process_loads(tmp_path):
    from scripts.hive import root_bus

    (tmp_path / ".env").write_text("")
    root_bus.write(tmp_path, _signer(), "root")

    environ = {"HOME": str(tmp_path / "home"), "AGENTIHOOKS_HOME": str(tmp_path)}
    assert operator_env.files(environ) == [tmp_path / ".env"]


@pytest.mark.feature("hive-root-bus-settings")
def test_a_signer_that_lists_member_swarms_without_root_mints_nothing(tmp_path):
    from scripts.hive import root_bus

    with pytest.raises(auth.HiveError) as refused:
        root_bus.write(tmp_path, _signer({"other": ("alpha",)}), "root")
    assert str(refused.value) == (
        "the hub record lists no member swarms for hive member root; it mints no unscoped bus credential"
    )
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
    monkeypatch.setattr(operator_env, "fill", _filled)
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


@pytest.mark.feature("hive-root-bus-settings")
def test_hive_root_bus_refuses_without_the_hub_signer(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    monkeypatch.setattr(operator_env, "fill", _filled)
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
    monkeypatch.setattr(operator_env, "fill", _filled)
    monkeypatch.setattr(scope, "member", lambda: "hive-7")
    monkeypatch.setattr(cli, "_signer", _signer)

    assert cli.main(["root-bus"]) == 1

    assert capsys.readouterr().err == (
        "hive root-bus refused: this host joined as hive member hive-7, whose hive env carries its bus settings\n"
    )
    assert not (tmp_path / "root-bus").exists()


@pytest.mark.feature("hive-root-bus-settings")
def test_a_scoped_signer_writes_the_root_swarms_into_the_bus_env(tmp_path):
    from scripts.hive import root_bus

    path = root_bus.write(tmp_path, _signer({"root": ("alpha", "beta")}), "root")

    assert dotenv_values(path, interpolate=False) == {
        "AGENTIHOOKS_BUS": "nats",
        "AGENTIHOOKS_BUS_URL": URL,
        "AGENTIHOOKS_BUS_CREDENTIALS": str(tmp_path / "root-bus" / "bus.creds"),
        "AGENTIHOOKS_BUS_BIND": "1",
        "AGENTIHOOKS_BUS_SCOPED": "1",
        "AGENTIHOOKS_BUS_SWARMS": "alpha,beta",
    }
    claims = nkey.claims(nkey.parse_creds((tmp_path / "root-bus" / "bus.creds").read_text())[0])
    assert "swarm.alpha.>" in claims["nats"]["sub"]["allow"]


@pytest.mark.feature("hive-root-bus-settings")
def test_hive_root_bus_lists_an_unlisted_root_in_the_signer_record_before_minting(tmp_path, monkeypatch, capsys):
    from scripts import openbao_kv
    from scripts.hive import member_swarms
    from scripts.swarm import commands

    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    monkeypatch.delenv("AGENTIHOOKS_HIVE_ID", raising=False)
    monkeypatch.setattr(operator_env, "fill", _filled)
    monkeypatch.setattr(commands, "hive_id", lambda: "root")
    signers = [_signer({"other": ("alpha",)}), _signer({"other": ("alpha",), "root": ("alpha",)})]
    monkeypatch.setattr(cli, "_signer", lambda: signers.pop(0))
    monkeypatch.setattr(cli, "redis_client", lambda: "redis")
    monkeypatch.setattr(openbao_kv, "from_env", _from_process("bao"))
    monkeypatch.setattr(member_swarms, "signer_path", _from_process("agentihooks/hub/nats"))
    monkeypatch.setattr(member_swarms, "sync_target", _from_process("ns/name"))
    monkeypatch.setattr(member_swarms, "sync", lambda target: [f"requested a sync of the signer record {target}"])
    chosen: list[tuple[str, list[str]]] = []

    def swarms(redis: str, named: list[str]) -> tuple[str, ...]:
        chosen.append((redis, named))
        return ("alpha",)

    monkeypatch.setattr(member_swarms, "swarms", swarms)
    assigned = []
    monkeypatch.setattr(
        member_swarms, "assign", lambda bao, path, members, swarms: assigned.append((bao, path, members, swarms))
    )
    monkeypatch.setattr(daemon, "install", lambda: True)
    monkeypatch.setattr(daemon, "restart", lambda: True)

    assert cli.main(["root-bus", "--swarm", "alpha"]) == 0

    assert chosen == [("redis", ["alpha"])]
    assert assigned == [("bao", "agentihooks/hub/nats", ["root"], ("alpha",))]
    path = registry.home() / "root-bus" / "bus.env"
    assert capsys.readouterr().out.splitlines() == [
        "listed hive root in the hub signer record",
        "requested a sync of the signer record ns/name",
        f"wrote the NATS bus settings of hive root to {path} and restarted the hive service",
    ]
    assert dotenv_values(path)["AGENTIHOOKS_BUS_SWARMS"] == "alpha"


@pytest.mark.feature("hive-root-bus-settings")
@pytest.mark.parametrize(("bao", "path"), [(None, "agentihooks/hub/nats"), ("bao", None)])
def test_hive_root_bus_refuses_an_unlisted_root_it_cannot_list(tmp_path, monkeypatch, capsys, bao, path):
    from scripts import openbao_kv
    from scripts.hive import member_swarms
    from scripts.swarm import commands

    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    monkeypatch.delenv("AGENTIHOOKS_HIVE_ID", raising=False)
    monkeypatch.setattr(operator_env, "fill", _filled)
    monkeypatch.setattr(commands, "hive_id", lambda: "root")
    monkeypatch.setattr(cli, "_signer", lambda: _signer({"other": ("alpha",)}))
    monkeypatch.setattr(openbao_kv, "from_env", lambda environ: bao)
    monkeypatch.setattr(member_swarms, "signer_path", lambda environ: path)

    assert cli.main(["root-bus"]) == 1

    assert capsys.readouterr().err == (
        "hive root-bus refused: the hub signer record lists no member swarms for root and this host cannot update it\n"
    )
    assert not (tmp_path / "root-bus").exists()


@pytest.mark.feature("hive-root-bus-settings")
@pytest.mark.parametrize("reread", [None, {}, {"other": ("alpha",)}])
def test_hive_root_bus_refuses_when_a_fresh_signer_read_still_lacks_root(tmp_path, monkeypatch, capsys, reread):
    from scripts import openbao_kv
    from scripts.hive import member_swarms
    from scripts.swarm import commands

    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    monkeypatch.delenv("AGENTIHOOKS_HIVE_ID", raising=False)
    monkeypatch.setattr(operator_env, "fill", _filled)
    monkeypatch.setattr(commands, "hive_id", lambda: "root")
    stale = None if reread is None else _signer(reread or None)
    signers = [_signer({"other": ("alpha",)}), stale]
    monkeypatch.setattr(cli, "_signer", lambda: signers.pop(0))
    monkeypatch.setattr(cli, "redis_client", lambda: "redis")
    monkeypatch.setattr(openbao_kv, "from_env", _from_process("bao"))
    monkeypatch.setattr(member_swarms, "signer_path", _from_process("agentihooks/hub/nats"))
    named: list[object] = []
    monkeypatch.setattr(member_swarms, "swarms", lambda redis, chosen: named.append(chosen) or ("alpha",))
    monkeypatch.setattr(member_swarms, "assign", lambda bao, path, members, swarms: list(members))
    monkeypatch.setattr(member_swarms, "sync", lambda target: [])

    assert cli.main(["root-bus"]) == 1

    assert named == [[]]
    assert capsys.readouterr().err == (
        "hive root-bus refused: listed hive root in the hub signer record, but a fresh read does not list it\n"
    )
    assert not (tmp_path / "root-bus").exists()


@pytest.mark.feature("hive-root-bus-settings")
def test_hive_root_bus_refuses_when_the_hive_service_does_not_restart(tmp_path, monkeypatch, capsys):
    from scripts.swarm import commands

    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    monkeypatch.delenv("AGENTIHOOKS_HIVE_ID", raising=False)
    monkeypatch.setattr(operator_env, "fill", _filled)
    monkeypatch.setattr(cli, "_signer", _signer)
    monkeypatch.setattr(commands, "hive_id", lambda: "root")
    monkeypatch.setattr(daemon, "install", lambda: True)
    monkeypatch.setattr(daemon, "restart", lambda: False)

    assert cli.main(["root-bus"]) == 1

    path = tmp_path / "root-bus" / "bus.env"
    assert capsys.readouterr().err == (
        f"hive root-bus refused: wrote {path}, but the hive user service did not restart\n"
    )


@pytest.mark.feature("hive-root-bus-settings")
def test_root_bus_writes_into_a_missing_home_and_again_on_a_rerun(tmp_path):
    from scripts.hive import root_bus

    home = tmp_path / "missing" / "home"
    first = root_bus.write(home, _signer(), "root")
    second = root_bus.write(home, _signer(), "root")

    assert first == second == home / "root-bus" / "bus.env"
    assert dotenv_values(second)["AGENTIHOOKS_BUS"] == "nats"


@pytest.mark.feature("hive-root-bus-settings")
def test_hive_help_names_the_root_bus_command_and_its_swarm_option(monkeypatch, capsys):
    monkeypatch.setenv("COLUMNS", "300")

    assert (
        "  Mint the root hive's NATS credential with the hub signer and write the bus env its service loads\n"
        in cli._parser().format_help()
    )
    with pytest.raises(SystemExit):
        cli.main(["root-bus", "--help"])
    assert "  --swarm SWARM  swarm to list root for; every swarm if omitted\n" in capsys.readouterr().out


def test_restart_restarts_the_hive_user_service():
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0)

    assert daemon.restart(run) is True
    assert calls == [
        (["systemctl", "--user", "restart", daemon.UNIT], {"capture_output": True, "text": True, "timeout": 30})
    ]
