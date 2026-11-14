import os
import stat
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import cast

import pytest
from dotenv import dotenv_values

from scripts import init_agent, openbao_kv, operator_env
from scripts.events import nkey
from scripts.events.config import BusConfig
from scripts.hive import auth, cli, daemon, member_swarms, nats_grant, root_bus
from scripts.swarm import commands

pytestmark = pytest.mark.unit

URL = "nats://hub.example:4222"
BAO = cast(openbao_kv.OpenBao, "bao")
ROOT = {"root": ("alpha",)}


def _signer(member_swarms: dict | None = None) -> nats_grant.Signer:
    operator, account = nkey.create(nkey.OPERATOR), nkey.create(nkey.ACCOUNT)
    return nats_grant.Signer(operator, account, "system", URL, operator.public, member_swarms)


def _filled(environ: object) -> list[str]:
    assert environ is os.environ
    return []


def _from_process(value: object) -> object:
    return lambda environ: value if environ is os.environ else None


def _root_host(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, hive: str = "root") -> Path:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("AGENTIHOOKS_HOME", raising=False)
    monkeypatch.delenv("AGENTIHOOKS_HIVE_ID", raising=False)
    monkeypatch.setattr(operator_env, "fill", _filled)
    monkeypatch.setattr(commands, "hive_id", lambda: hive)
    monkeypatch.setattr(cli, "redis_client", lambda: "redis")
    return tmp_path / ".agentihooks"


def _service(monkeypatch: pytest.MonkeyPatch, steps: list[str], restarted: bool = True) -> None:
    monkeypatch.setattr(root_bus, "verify", lambda path: steps.append(f"verify {path}"))
    monkeypatch.setattr(daemon, "install", lambda: steps.append("install") or True)
    monkeypatch.setattr(daemon, "restart", lambda: steps.append("restart") or restarted)


def _record(monkeypatch: pytest.MonkeyPatch, signers: list, extended: list, sync: list[str]) -> None:
    monkeypatch.setattr(cli, "_signer", lambda: signers.pop(0))
    monkeypatch.setattr(openbao_kv, "from_env", _from_process("bao"))
    monkeypatch.setattr(member_swarms, "signer_path", _from_process("agentihooks/hub/nats"))
    monkeypatch.setattr(member_swarms, "sync_target", _from_process("ns/name"))
    monkeypatch.setattr(member_swarms, "sync", lambda target: [f"{line} {target}" for line in sync])

    def extend(bao: str, path: str, member: str, chosen: tuple[str, ...]) -> list[str]:
        extended.append((bao, path, member, chosen))
        return ["beta", "gamma"]

    monkeypatch.setattr(member_swarms, "extend", extend)


def _swarms(monkeypatch: pytest.MonkeyPatch, named: list, chosen: tuple[str, ...]) -> None:
    def swarms(redis: str, asked: list[str]) -> tuple[str, ...]:
        named.append((redis, asked))
        return chosen

    monkeypatch.setattr(member_swarms, "swarms", swarms)


@pytest.mark.feature("hive-root-bus-settings")
def test_the_root_bus_env_resolves_to_nats_with_a_root_member_credential(tmp_path):
    signer = _signer(ROOT)
    path = root_bus.write(tmp_path, signer, "root")

    assert path == tmp_path / "root-bus" / "bus.env"
    values = {key: value or "" for key, value in dotenv_values(path, interpolate=False).items()}
    creds = tmp_path / "root-bus" / "bus.creds"
    assert values == {
        "AGENTIHOOKS_BUS": "nats",
        "AGENTIHOOKS_BUS_URL": URL,
        "AGENTIHOOKS_BUS_CREDENTIALS": str(creds),
        "AGENTIHOOKS_BUS_BIND": "1",
        "AGENTIHOOKS_BUS_SCOPED": "1",
        "AGENTIHOOKS_BUS_SWARMS": "alpha",
    }
    config = BusConfig.from_environ(values)
    assert (config.backend, config.url, config.provision) == ("nats", URL, False)
    jwt, user = nkey.parse_creds(config.credentials or "")
    claims = nkey.claims(jwt)
    assert claims["name"] == "hive-root"
    assert claims["iss"] == signer.account.public
    assert claims["sub"] == user.public == nats_grant.member_user(signer, "root").public
    assert claims["nats"] == nats_grant.member_claims(user.public, ("alpha",), member="root")
    assert stat.S_IMODE((tmp_path / "root-bus").stat().st_mode) == 0o700
    assert stat.S_IMODE(creds.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.feature("hive-root-bus-settings")
def test_the_root_bus_settings_stay_out_of_the_env_files_every_process_loads(tmp_path):
    (tmp_path / ".env").write_text("")
    root_bus.write(tmp_path, _signer(ROOT), "root")

    environ = {"HOME": str(tmp_path / "home"), "AGENTIHOOKS_HOME": str(tmp_path)}
    assert operator_env.files(environ) == [tmp_path / ".env"]


@pytest.mark.feature("hive-root-bus-settings")
def test_a_signer_that_lists_member_swarms_without_root_mints_nothing(tmp_path):
    with pytest.raises(auth.HiveError) as refused:
        root_bus.write(tmp_path, _signer({"other": ("alpha",)}), "root")
    assert str(refused.value) == (
        "the hub record lists no member swarms for hive member root; it mints no unscoped bus credential"
    )
    assert not (tmp_path / "root-bus").exists()


@pytest.mark.feature("hive-mint-refuses-unscoped-signer")
def test_a_signer_record_without_member_swarms_mints_no_root_credential(tmp_path):
    with pytest.raises(auth.HiveError) as refused:
        root_bus.write(tmp_path, _signer(), "root")
    assert str(refused.value) == (
        "the hub signer record has no member_swarms field, so it scopes no member; it mints no unscoped bus credential"
    )
    assert not (tmp_path / "root-bus").exists()


@pytest.mark.feature("hive-root-bus-settings")
def test_a_scoped_signer_writes_the_root_swarms_into_the_bus_env(tmp_path):
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
def test_a_credential_past_the_nats_control_line_is_refused_before_writing(tmp_path, monkeypatch):
    limit = root_bus.CONTROL_LINE - root_bus.CONNECT_ROOM
    monkeypatch.setattr(nats_grant, "mint", lambda signer, member: "x" * (limit + 1))

    with pytest.raises(auth.HiveError) as refused:
        root_bus.write(tmp_path, _signer({"root": ("alpha", "beta")}), "root")

    assert str(refused.value) == (
        f"the root credential is {limit + 1} bytes for 2 swarms, past the 65536 byte NATS control line; "
        "name fewer swarms with --swarm"
    )
    assert not (tmp_path / "root-bus").exists()
    monkeypatch.setattr(nats_grant, "mint", lambda signer, member: "x" * limit)
    assert root_bus.write(tmp_path, _signer(ROOT), "root").is_file()


@pytest.mark.feature("hive-root-bus-settings")
def test_root_bus_writes_into_a_missing_home_and_again_on_a_rerun(tmp_path):
    home = tmp_path / "missing" / "home"
    first = root_bus.write(home, _signer(ROOT), "root")
    second = root_bus.write(home, _signer(ROOT), "root")

    assert first == second == home / "root-bus" / "bus.env"
    assert dotenv_values(second)["AGENTIHOOKS_BUS"] == "nats"


@pytest.mark.feature("hive-root-bus-settings")
def test_verify_connects_once_with_the_written_bus_env_and_closes(tmp_path, monkeypatch):
    from scripts.events import nats

    path = root_bus.write(tmp_path, _signer({"root": ("alpha",)}), "root")
    seen: list[object] = []

    class Bus:
        def close(self) -> None:
            seen.append("closed")

    def open_bus(config: BusConfig) -> Bus:
        seen.append(config)
        return Bus()

    monkeypatch.setattr(nats, "open_bus", open_bus)

    root_bus.verify(path)

    config, closed = seen
    assert closed == "closed"
    assert isinstance(config, BusConfig)
    assert (config.backend, config.url, config.provision, config.scoped) == ("nats", URL, False, True)
    assert config.credentials == (tmp_path / "root-bus" / "bus.creds").read_text()


@pytest.mark.feature("hive-root-bus-settings")
def test_verify_turns_a_hub_refusal_into_a_hive_error(tmp_path, monkeypatch):
    from scripts.events import nats

    path = root_bus.write(tmp_path, _signer(ROOT), "root")

    def refuse(config: BusConfig) -> None:
        raise ConnectionResetError("maximum control line exceeded")

    monkeypatch.setattr(nats, "open_bus", refuse)

    with pytest.raises(auth.HiveError) as refused:
        root_bus.verify(path)
    assert str(refused.value) == (
        f"the hub refused the root hive credential in {path} (ConnectionResetError: maximum control line exceeded)"
    )


@pytest.mark.feature("hive-root-bus-settings")
def test_remove_deletes_the_root_bus_files(tmp_path):
    path = root_bus.write(tmp_path, _signer(ROOT), "root")

    assert root_bus.remove(tmp_path) is True
    assert not path.exists()
    assert not (tmp_path / "root-bus" / "bus.creds").exists()
    assert root_bus.remove(tmp_path) is False


@pytest.mark.feature("hive-root-bus-settings")
def test_the_hive_service_loads_the_root_bus_env_after_the_hive_env():
    text = daemon.unit("/bin/agentihooks")

    hive = text.index("EnvironmentFile=-%h/.agentihooks/hive.env\n")
    root = text.index("EnvironmentFile=-%h/.agentihooks/root-bus/bus.env\n")
    assert hive < root < text.index("ExecStart=")


@pytest.mark.feature("hive-root-bus-settings")
def test_hive_root_bus_writes_verifies_then_reinstalls_and_restarts_the_service(tmp_path, monkeypatch, capsys):
    home = _root_host(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "_signer", lambda: _signer(ROOT))
    _swarms(monkeypatch, [], ("alpha",))
    steps: list[str] = []
    _service(monkeypatch, steps)

    assert cli.main(["root-bus"]) == 0

    path = home / "root-bus" / "bus.env"
    assert steps == [f"verify {path}", "install", "restart"]
    assert dotenv_values(path)["AGENTIHOOKS_BUS"] == "nats"
    assert capsys.readouterr().out == (
        f"wrote the NATS bus settings of hive root to {path} and restarted the hive service\n"
    )


@pytest.mark.feature("hive-root-bus-settings")
def test_hive_root_bus_extends_the_root_listing_with_missing_swarms(tmp_path, monkeypatch, capsys):
    home = _root_host(monkeypatch, tmp_path)
    signers = [_signer({"root": ("alpha",)}), _signer({"root": ("alpha", "beta")})]
    extended: list = []
    _record(monkeypatch, signers, extended, ["requested a sync of the signer record"])
    named: list = []
    _swarms(monkeypatch, named, ("alpha", "beta"))
    _service(monkeypatch, [])

    assert cli.main(["root-bus", "--swarm", "alpha", "--swarm", "beta"]) == 0

    assert named == [("redis", ["alpha", "beta"])]
    assert extended == [("bao", "agentihooks/hub/nats", "root", ("alpha", "beta"))]
    path = home / "root-bus" / "bus.env"
    assert capsys.readouterr().out.splitlines() == [
        "listed hive root for beta, gamma in the hub signer record",
        "requested a sync of the signer record ns/name",
        f"wrote the NATS bus settings of hive root to {path} and restarted the hive service",
    ]
    assert dotenv_values(path)["AGENTIHOOKS_BUS_SWARMS"] == "alpha,beta"


@pytest.mark.feature("hive-root-bus-settings")
def test_hive_root_bus_keeps_a_listing_that_covers_every_wanted_swarm(tmp_path, monkeypatch, capsys):
    home = _root_host(monkeypatch, tmp_path)
    extended: list = []
    _record(monkeypatch, [_signer({"root": ("alpha", "beta")})], extended, [])
    named: list = []
    _swarms(monkeypatch, named, ("beta", "alpha"))
    _service(monkeypatch, [])

    assert cli.main(["root-bus"]) == 0

    assert named == [("redis", [])]
    assert extended == []
    path = home / "root-bus" / "bus.env"
    assert capsys.readouterr().out == (
        f"wrote the NATS bus settings of hive root to {path} and restarted the hive service\n"
    )


@pytest.mark.feature("hive-root-bus-settings")
@pytest.mark.parametrize(("bao", "path"), [(None, "agentihooks/hub/nats"), ("bao", None)])
def test_hive_root_bus_refuses_an_unlisted_root_it_cannot_list(tmp_path, monkeypatch, capsys, bao, path):
    home = _root_host(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "_signer", lambda: _signer({"other": ("alpha",)}))
    _swarms(monkeypatch, [], ("alpha",))
    monkeypatch.setattr(openbao_kv, "from_env", lambda environ: bao)
    monkeypatch.setattr(member_swarms, "signer_path", lambda environ: path)

    assert cli.main(["root-bus"]) == 1

    assert capsys.readouterr().err == (
        "hive root-bus refused: the hub signer record does not list root for every swarm "
        "and this host cannot update it\n"
    )
    assert not (home / "root-bus").exists()


@pytest.mark.feature("hive-root-bus-settings")
@pytest.mark.parametrize("reread", [None, {}, {"other": ("alpha",)}, {"root": ("alpha",)}])
def test_hive_root_bus_refuses_when_a_fresh_signer_read_still_lacks_a_swarm(tmp_path, monkeypatch, capsys, reread):
    home = _root_host(monkeypatch, tmp_path)
    stale = None if reread is None else _signer(reread or None)
    _record(monkeypatch, [_signer({"other": ("alpha",)}), stale], [], [])
    _swarms(monkeypatch, [], ("alpha", "beta"))

    assert cli.main(["root-bus"]) == 1

    assert capsys.readouterr().err == (
        "hive root-bus refused: listed hive root in the hub signer record, but a fresh read does not list it\n"
    )
    assert not (home / "root-bus").exists()


@pytest.mark.feature("hive-root-bus-settings")
def test_hive_root_bus_refuses_without_the_hub_signer(tmp_path, monkeypatch, capsys):
    home = _root_host(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "_signer", lambda: None)

    assert cli.main(["root-bus"]) == 1

    assert capsys.readouterr().err == (
        "hive root-bus refused: AGENTIHOOKS_NATS_SIGNER must name the hub NATS signer to mint the root hive credential\n"
    )
    assert not (home / "root-bus").exists()


@pytest.mark.feature("hive-root-bus-settings")
def test_hive_root_bus_refuses_on_a_joined_member_host(tmp_path, monkeypatch, capsys):
    from scripts.hive import scope

    home = _root_host(monkeypatch, tmp_path)
    monkeypatch.setattr(scope, "member", lambda: "hive-7")
    monkeypatch.setattr(cli, "_signer", _signer)

    assert cli.main(["root-bus"]) == 1

    assert capsys.readouterr().err == (
        "hive root-bus refused: this host joined as hive member hive-7, whose hive env carries its bus settings\n"
    )
    assert not (home / "root-bus").exists()


@pytest.mark.feature("hive-root-bus-settings")
def test_hive_root_bus_refuses_a_hive_other_than_root(tmp_path, monkeypatch, capsys):
    home = _root_host(monkeypatch, tmp_path, hive="worker-3")
    monkeypatch.setattr(cli, "_signer", _signer)

    assert cli.main(["root-bus"]) == 1

    assert capsys.readouterr().err == (
        "hive root-bus refused: hive root-bus serves the root hive only, and this host is hive worker-3\n"
    )
    assert not (home / "root-bus").exists()


@pytest.mark.feature("hive-root-bus-settings")
def test_hive_root_bus_refuses_a_home_the_hive_service_does_not_load(tmp_path, monkeypatch, capsys):
    _root_host(monkeypatch, tmp_path)
    elsewhere = tmp_path / "elsewhere"
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(elsewhere))
    monkeypatch.setattr(cli, "_signer", _signer)

    assert cli.main(["root-bus"]) == 1

    assert capsys.readouterr().err == (
        f"hive root-bus refused: the hive user service loads its env from ~/.agentihooks, not {elsewhere}\n"
    )
    assert not elsewhere.exists()


@pytest.mark.feature("hive-root-bus-settings")
def test_hive_root_bus_refuses_when_the_hive_service_does_not_restart(tmp_path, monkeypatch, capsys):
    home = _root_host(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "_signer", lambda: _signer(ROOT))
    _swarms(monkeypatch, [], ("alpha",))
    _service(monkeypatch, [], restarted=False)

    assert cli.main(["root-bus"]) == 1

    path = home / "root-bus" / "bus.env"
    assert (
        capsys.readouterr().err == f"hive root-bus refused: wrote {path}, but the hive user service did not restart\n"
    )


@pytest.mark.feature("hive-root-bus-settings")
def test_hive_root_bus_refuses_when_systemctl_fails(tmp_path, monkeypatch, capsys):
    home = _root_host(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "_signer", lambda: _signer(ROOT))
    _swarms(monkeypatch, [], ("alpha",))
    _service(monkeypatch, [])

    def fail() -> bool:
        raise subprocess.CalledProcessError(1, ["systemctl", "--user", "daemon-reload"])

    monkeypatch.setattr(daemon, "install", fail)

    assert cli.main(["root-bus"]) == 1

    path = home / "root-bus" / "bus.env"
    assert capsys.readouterr().err == (
        f"hive root-bus refused: wrote {path}, but the hive user service did not restart "
        "(Command '['systemctl', '--user', 'daemon-reload']' returned non-zero exit status 1.)\n"
    )


@pytest.mark.feature("hive-root-bus-settings")
def test_hive_revoke_root_removes_the_root_bus_files(tmp_path, monkeypatch, capsys):
    home = _root_host(monkeypatch, tmp_path)
    root_bus.write(home, _signer(ROOT), "root")
    monkeypatch.setattr(cli, "_signer", lambda: None)
    monkeypatch.setattr(cli, "_unlist", lambda member: [])
    revoked: list[str] = []
    monkeypatch.setattr(auth, "revoke", lambda redis, member: revoked.append(member))

    assert cli.main(["revoke", "root"]) == 0
    assert cli.main(["revoke", "hive-7"]) == 0

    assert revoked == ["hive-7"]
    assert not (home / "root-bus" / "bus.env").exists()
    assert capsys.readouterr().out == "revoked root\nrevoked hive-7\n"


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


@pytest.mark.feature("hive-root-bus-settings")
def test_a_root_spoke_agent_inherits_the_bus_credential_reference():
    environ = {
        "AGENTIHOOKS_SWARM": "rig",
        "AGENTIHOOKS_BUS": "nats",
        "AGENTIHOOKS_BUS_URL": URL,
        "AGENTIHOOKS_BUS_CREDENTIALS": "/home/op/.agentihooks/root-bus/bus.creds",
        "AGENTIHOOKS_BUS_BIND": "1",
        "AGENTIHOOKS_BUS_SCOPED": "1",
        "AGENTIHOOKS_BUS_SWARMS": "rig,alpha",
    }
    script = init_agent._swarm_exports(environ) + 'printf "%s|%s|%s|%s" "$AGENTIHOOKS_BUS_CREDENTIALS" '
    script += '"$AGENTIHOOKS_BUS_BIND" "$AGENTIHOOKS_BUS_SCOPED" "$AGENTIHOOKS_BUS_SWARMS"'

    result = subprocess.run(["bash", "-c", script], env={}, capture_output=True, text=True, check=True)

    assert result.stdout == "/home/op/.agentihooks/root-bus/bus.creds|1|1|rig,alpha"


@pytest.mark.feature("hive-root-bus-settings")
def test_an_inline_bus_credential_never_reaches_the_launcher_script():
    from scripts.events.config import INLINE_CREDENTIALS

    environ = {"AGENTIHOOKS_SWARM": "rig", "AGENTIHOOKS_BUS_CREDENTIALS": f"{INLINE_CREDENTIALS}\nsecret"}

    assert "AGENTIHOOKS_BUS_CREDENTIALS" not in init_agent._swarm_exports(environ)
    assert "AGENTIHOOKS_BUS_CREDENTIALS" not in init_agent._swarm_exports({"AGENTIHOOKS_SWARM": "rig"})


def _bao_record(monkeypatch: pytest.MonkeyPatch, record: dict) -> list[dict]:
    writes: list[dict] = []

    def update(bao: str, path: str, change: Callable[[dict], Mapping]) -> int:
        assert (bao, path) == ("bao", "agentihooks/hub/nats")
        entries = dict(change(record))
        writes.append(entries)
        record.update(entries)
        return len(record)

    monkeypatch.setattr(openbao_kv, "update", update)
    return writes


@pytest.mark.feature("hive-root-bus-settings")
def test_extend_adds_only_the_missing_swarms_and_keeps_other_members(monkeypatch):
    record = {"url": "nats://x", "member_swarms": {"root": ["alpha"], "other": ["gamma"]}}
    writes = _bao_record(monkeypatch, record)

    added = member_swarms.extend(BAO, "agentihooks/hub/nats", "root", ("alpha", "beta", "delta"))

    assert added == ["beta", "delta"]
    assert writes == [{"member_swarms": {"root": ["alpha", "beta", "delta"], "other": ["gamma"]}}]


@pytest.mark.feature("hive-root-bus-settings")
def test_extend_lists_an_absent_member_and_writes_nothing_when_covered(monkeypatch):
    record: dict = {"url": "nats://x"}
    writes = _bao_record(monkeypatch, record)

    assert member_swarms.extend(BAO, "agentihooks/hub/nats", "root", ("alpha",)) == ["alpha"]
    assert member_swarms.extend(BAO, "agentihooks/hub/nats", "root", ("alpha",)) == []

    assert writes == [{"member_swarms": {"root": ["alpha"]}}, {}]


@pytest.mark.feature("hive-root-bus-settings")
def test_extend_turns_an_openbao_failure_into_a_hive_error(monkeypatch):
    def update(bao: str, path: str, change: object) -> int:
        raise openbao_kv.OpenBaoError(403, "permission denied")

    monkeypatch.setattr(openbao_kv, "update", update)

    with pytest.raises(auth.HiveError) as refused:
        member_swarms.extend(BAO, "agentihooks/hub/nats", "root", ("alpha",))
    assert str(refused.value) == "OpenBao HTTP 403: permission denied"


@pytest.mark.feature("hive-root-bus-settings")
def test_extend_refuses_an_invalid_swarm_name(monkeypatch):
    _bao_record(monkeypatch, {"member_swarms": {}})

    with pytest.raises(auth.HiveError) as refused:
        member_swarms.extend(BAO, "agentihooks/hub/nats", "root", ("bad name",))
    assert str(refused.value).startswith("member_swarms contains an invalid swarm name")


def test_restart_restarts_the_hive_user_service():
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0)

    assert daemon.restart(run) is True
    assert calls == [
        (["systemctl", "--user", "restart", daemon.UNIT], {"capture_output": True, "text": True, "timeout": 30})
    ]
