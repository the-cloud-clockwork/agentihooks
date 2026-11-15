"""Hive membership changes request signer refreshes and join sees the reconciled listing."""

import json
import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from scripts import openbao_kv
from scripts.events import nkey
from scripts.hive import auth, cli, invite, member_swarms, nats_grant, remove
from tests.codex_fakes import BAO_TOKEN, BAO_URL
from tests.hive.test_invite import RECORD, URL, Bao
from tests.hive.test_nats_grant import _refresh

pytestmark = pytest.mark.unit

SIGNER = "agentihooks/hub/nats-signer"
TARGET = "agentihooks-swarm/agentihooks-swarm-nats-signer"
PUBLIC = "rediss://hive.example:6380/2"
SYNCED = f"requested a sync of the signer record {TARGET}"
SIGNER_FIELDS = ("operator_seed", "account_seed", "system_credentials", "url")


@pytest.fixture
def admin() -> Any:
    import fakeredis

    return fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)


class Eso:
    """Separate an acknowledged sync request from the later signer mount projection."""

    def __init__(self, fake: Bao, mount: Path) -> None:
        self.fake = fake
        self.mount = mount
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert kwargs.get("capture_output") is True
        assert kwargs.get("text") is True
        self.commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    def copy(self) -> None:
        """Write each field of the signer record as one mounted file, the way the synced record lands."""
        for key, value in self.fake.records[SIGNER].items():
            (self.mount / key).write_text(json.dumps(value) if key == member_swarms.FIELD else value)


def _record(listed: dict[str, list[str]]) -> tuple[dict[str, Any], str]:
    operator, account = nkey.create(nkey.OPERATOR), nkey.create(nkey.ACCOUNT)
    record: dict[str, Any] = dict(zip(SIGNER_FIELDS, (operator.seed, account.seed, "system", "nats://127.0.0.1:1")))
    record[member_swarms.FIELD] = listed
    return record, operator.public


def _mounted(mount: Path, trusted: str) -> Callable[[], nats_grant.Signer | None]:
    environ = {nats_grant.SIGNER_ENV: f"file:{mount}", nats_grant.TRUSTED_OPERATOR_ENV: trusted}
    return lambda: nats_grant.from_environ(environ)


def _scoped(target: str | None = TARGET) -> invite.Request:
    return invite.Request("lab", RECORD, url=URL, redis_url=PUBLIC, signer_path=SIGNER, swarms=("rig",), sync=target)


def _ledger_credential(fake: Bao) -> str:
    return invite._env(fake.records[RECORD]["credential"])["AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL"]


def _failing(code: int, stderr: str) -> Callable[..., subprocess.CompletedProcess[str]]:
    def run(command: list[str], check: bool = False, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if check and code:
            raise subprocess.CalledProcessError(code, command, stderr=stderr)
        return subprocess.CompletedProcess(command, code, "", stderr)

    return run


def _missing(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    raise FileNotFoundError("kubectl")


@pytest.mark.feature("hive-signer-membership-sync")
def test_an_invited_member_joins_at_once_through_the_mounted_signer(admin: Any, tmp_path: Path) -> None:
    record, trusted = _record({"m0": ["old"]})
    fake = Bao({SIGNER: record})
    eso = Eso(fake, tmp_path)
    eso.copy()
    said = invite.invite(admin, openbao_kv.OpenBao(BAO_URL, BAO_TOKEN, fake), _scoped(), PUBLIC, run=eso)
    assert said.endswith(f"; {SYNCED}")
    stale = _refresh(admin, _mounted(tmp_path, trusted), _ledger_credential(fake))
    assert "error" in stale
    eso.copy()
    grant = _refresh(admin, _mounted(tmp_path, trusted), _ledger_credential(fake))
    assert (grant["bus_scoped"], grant["bus_swarms"]) == (True, ["rig"])
    ((*command, stamp, overwrite),) = eso.commands
    assert command == [
        "kubectl",
        "annotate",
        "externalsecret",
        "agentihooks-swarm-nats-signer",
        "--namespace",
        "agentihooks-swarm",
    ]
    name, _, value = stamp.partition("=")
    assert (name, value.isdigit(), overwrite) == ("force-sync", True, "--overwrite")
    assert said.endswith(f"; {SYNCED}")


@pytest.mark.parametrize(
    ("run", "reason"),
    [(_failing(1, "forbidden\n"), "forbidden"), (_failing(2, ""), "exit 2"), (_missing, "kubectl")],
)
def test_a_failed_sync_is_reported_and_the_invite_stands(
    admin: Any, run: Callable[..., subprocess.CompletedProcess[str]], reason: str
) -> None:
    record, _ = _record({})
    fake = Bao({SIGNER: record})
    said = invite.invite(admin, openbao_kv.OpenBao(BAO_URL, BAO_TOKEN, fake), _scoped(), PUBLIC, run=run)
    assert said.endswith(f"; could not force the sync of {TARGET} ({reason}), so hive join sees it at the next refresh")
    assert len(auth.members(admin)) == 1


def test_an_invite_without_a_sync_target_or_a_scoped_record_runs_no_sync(admin: Any) -> None:
    runs: list[list[str]] = []
    record, _ = _record({})
    fake = Bao({SIGNER: record})
    bao = openbao_kv.OpenBao(BAO_URL, BAO_TOKEN, fake)
    invite.invite(
        admin,
        bao,
        _scoped(None),
        PUBLIC,
        run=lambda command, **kwargs: runs.append(command) or subprocess.CompletedProcess(command, 0),
    )
    del fake.records[SIGNER][member_swarms.FIELD]
    with pytest.raises(auth.HiveError):
        invite.invite(
            admin,
            bao,
            _scoped(),
            PUBLIC,
            run=lambda command, **kwargs: runs.append(command) or subprocess.CompletedProcess(command, 0),
        )
    assert runs == []


def test_the_sync_target_defaults_for_the_hub_record_and_an_empty_setting_turns_it_off() -> None:
    hub = {nats_grant.SIGNER_ENV: SIGNER}
    assert member_swarms.sync_target(hub) == TARGET
    assert member_swarms.sync_target({**hub, member_swarms.SYNC_ENV: "ns/other"}) == "ns/other"
    assert member_swarms.sync_target({**hub, member_swarms.SYNC_ENV: ""}) is None
    assert member_swarms.sync_target({nats_grant.SIGNER_ENV: "file:/nats-signer"}) is None
    assert member_swarms.sync_target({}) is None


@pytest.fixture
def revoking(admin: Any, monkeypatch: pytest.MonkeyPatch) -> tuple[Bao, list[list[str]]]:
    fake, runs = Bao({SIGNER: {"url": "kept", member_swarms.FIELD: {"m2": ["rig"]}}}), []
    monkeypatch.setenv(nats_grant.SIGNER_ENV, SIGNER)
    monkeypatch.setenv(openbao_kv.URL_ENV, BAO_URL)
    monkeypatch.setenv(openbao_kv.TOKEN_ENV, BAO_TOKEN)
    monkeypatch.delenv(member_swarms.SYNC_ENV, raising=False)
    monkeypatch.setattr(cli, "redis_client", lambda: admin)
    monkeypatch.setattr(cli, "_signer", lambda: None)

    def from_env(environ: Any) -> openbao_kv.OpenBao:
        assert environ is os.environ
        return openbao_kv.OpenBao(BAO_URL, BAO_TOKEN, fake)

    monkeypatch.setattr(openbao_kv, "from_env", from_env)
    host = remove.Host
    monkeypatch.setattr(
        remove,
        "Host",
        lambda: host(
            run=lambda command, **kwargs: (
                runs.append(command)
                or subprocess.CompletedProcess(command, 0)
                or subprocess.CompletedProcess(command, 0)
            )
        ),
    )
    return fake, runs


@pytest.mark.feature("hive-signer-membership-sync")
def test_hive_revoke_drops_the_member_from_the_signer_listing_and_syncs(
    admin: Any, revoking: tuple[Bao, list[list[str]]], capsys: pytest.CaptureFixture[str]
) -> None:
    fake, runs = revoking
    member = auth.mint(admin, "h1", PUBLIC)["id"]
    member_swarms.assign(openbao_kv.OpenBao(BAO_URL, BAO_TOKEN, fake), SIGNER, [member], ("rig",))
    assert cli.main(["revoke", member]) == 0
    assert fake.records[SIGNER] == {"url": "kept", member_swarms.FIELD: {"m2": ["rig"]}}
    unlisted = f"unlisted {member} from the hub signer record {SIGNER}"
    assert capsys.readouterr().out == f"{unlisted}\n{SYNCED}\nrevoked {member}\n"
    assert [command[3] for command in runs] == ["agentihooks-swarm-nats-signer"]
    assert auth.member(admin, member) is None


def test_hive_revoke_of_an_unlisted_member_writes_and_syncs_nothing(
    admin: Any, revoking: tuple[Bao, list[list[str]]], capsys: pytest.CaptureFixture[str]
) -> None:
    fake, runs = revoking
    member = auth.mint(admin, "h1", PUBLIC)["id"]
    assert cli.main(["revoke", member]) == 0
    unlisted = f"the hub signer record {SIGNER} lists no member swarms for {member}"
    assert capsys.readouterr().out == f"{unlisted}\nrevoked {member}\n"
    assert (fake.written, runs) == ([], [])


def test_hive_revoke_with_a_hub_signer_but_no_openbao_refuses_before_revoking(
    admin: Any,
    revoking: tuple[Bao, list[list[str]]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(openbao_kv, "from_env", lambda environ: None)
    member = auth.mint(admin, "h1", PUBLIC)["id"]
    assert cli.main(["revoke", member]) == 1
    assert capsys.readouterr().err == f"hive revoke refused: no OpenBao client to unlist {member} from {SIGNER}\n"
    assert auth.member(admin, member) is not None


@pytest.mark.feature("hive-signer-membership-sync")
def test_hive_remove_syncs_the_signer_record_after_it_unlists() -> None:
    fake = Bao({SIGNER: {member_swarms.FIELD: {"m1": ["rig"]}}})
    runs: list[list[str]] = []
    plan = remove.Plan("m1", "anton-9", "k8s/agentihooks-hive-anton-9", signer_path=SIGNER, sync=TARGET)
    host = remove.Host(
        run=lambda command, **kwargs: (
            runs.append(command) or subprocess.CompletedProcess(command, 0) or subprocess.CompletedProcess(command, 0)
        )
    )
    bao = openbao_kv.OpenBao(BAO_URL, BAO_TOKEN, fake)
    step = dict(remove.STEPS)["signer"]
    assert step(None, bao, plan, host) == [f"unlisted m1 from the hub signer record {SIGNER}", SYNCED]
    assert step(None, bao, plan, host) == [f"the hub signer record {SIGNER} lists no member swarms for m1"]
    assert len(runs) == 1


def test_the_remove_command_names_the_sync_target(admin: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "redis_client", lambda: admin)
    monkeypatch.setattr(cli, "_signer", lambda: None)
    monkeypatch.setenv(openbao_kv.URL_ENV, BAO_URL)
    monkeypatch.setenv(openbao_kv.TOKEN_ENV, BAO_TOKEN)
    monkeypatch.setenv(nats_grant.SIGNER_ENV, SIGNER)
    monkeypatch.delenv(member_swarms.SYNC_ENV, raising=False)
    seen: list[remove.Plan] = []
    monkeypatch.setattr(remove, "remove", lambda store, bao, plan, host: seen.append(plan) or [])
    assert cli.main(["remove", "m1", "--label", "anton-9"]) == 0
    assert [plan.sync for plan in seen] == [TARGET]


@pytest.mark.feature("hive-signer-membership-sync")
def test_the_invite_command_passes_the_signer_sync_target(
    admin: Any,
    revoking: tuple[Bao, list[list[str]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[invite.Request] = []
    monkeypatch.setattr(
        invite, "invite", lambda redis, bao, request, local_redis_url: seen.append(request) or "invited"
    )
    assert cli.main(["invite", "lab", "--url", URL, "--swarm", "rig"]) == 0
    assert [request.sync for request in seen] == [TARGET]
