import subprocess

import pytest
import yaml

from scripts import openbao_kv
from scripts.events import nkey
from scripts.events.config import BusConfig
from scripts.hive import auth, cli, nats_grant, nats_trust
from tests.events import nats_server

pytestmark = pytest.mark.unit

URL = "tls://nats.example:4222"
WEBHOOK_PUBLISH = ["swarm.*.pull.opened", "$KV.hub-github.>"]
USERS = [
    {"user": "swarm-controller", "openbao": "k8s/agentihooks-swarm-nats-controller"},
    {
        "user": "swarm-github-webhook",
        "openbao": "k8s/agentihooks-swarm-nats-github-webhook",
        "permissions": {"publish": {"allow": WEBHOOK_PUBLISH}, "subscribe": {"allow": ["_INBOX.>"]}},
    },
    {
        "user": "swarm-ledger",
        "openbao": "k8s/agentihooks-swarm-nats-ledger",
        "permissions": {"publish": ["swarm.>"], "subscribe": {"allow": ["swarm.>"], "deny": ["swarm.x"]}},
    },
]


@pytest.fixture
def bao(monkeypatch):
    records: dict[str, dict] = {"k8s/agentihooks-swarm-nats-controller": {"password": "old"}}

    def read(found, path):
        assert found.url == "https://bao.example"
        return dict(records.get(path, {})), len(records.get(path, {}))

    def merge(found, path, entries, stale=()):
        records[path] = {**records.get(path, {}), **entries}
        return len(records[path])

    monkeypatch.setattr(openbao_kv, "read", read)
    monkeypatch.setattr(openbao_kv, "merge", merge)
    return openbao_kv.OpenBao("https://bao.example", "t"), records


def _signer(records: dict) -> nats_grant.Signer:
    record = records[nats_trust.SIGNER_PATH]
    return nats_grant.Signer(
        nkey.from_seed(record["operator_seed"]),
        nkey.from_seed(record["account_seed"]),
        record["system_credentials"],
        record["url"],
        nkey.from_seed(record["operator_seed"]).public,
    )


def test_the_signer_record_lives_under_the_hub_path():
    assert nats_trust.SIGNER_PATH == "agentihooks/hub/nats-signer"


def test_bootstrap_creates_the_keys_once_and_keeps_them(bao):
    client, records = bao
    _, lines = nats_trust.bootstrap(client, USERS, URL)
    first = dict(records[nats_trust.SIGNER_PATH])
    _, again = nats_trust.bootstrap(client, USERS, "tls://elsewhere:4222")

    assert records[nats_trust.SIGNER_PATH] == first
    assert first["url"] == URL
    assert sorted(first) == ["account_seed", "operator_seed", "system_credentials", "system_seed", "url"]
    operator, system, account = (nkey.from_seed(first[f"{name}_seed"]) for name in ("operator", "system", "account"))
    assert (operator.public[0], system.public[0], account.public[0]) == ("O", "A", "A")
    assert lines[0] == again[0] == f"operator {operator.public}, swarm account {account.public}"
    jwt, user = nkey.parse_creds(first["system_credentials"])
    assert user == nkey.derive(system, nkey.USER, "system")
    assert nkey.claims(jwt) | {"jti": "", "iat": 0} == {
        "jti": "",
        "iat": 0,
        "iss": system.public,
        "name": "system",
        "sub": user.public,
        "nats": nats_grant.user_claims(),
    }


def test_each_service_user_is_signed_into_its_openbao_record(bao):
    client, records = bao
    _, lines = nats_trust.bootstrap(client, USERS, URL)
    account = nkey.from_seed(records[nats_trust.SIGNER_PATH]["account_seed"])

    assert records["k8s/agentihooks-swarm-nats-controller"]["password"] == "old"
    assert lines[1:] == [
        "swarm-controller: OpenBao secret/k8s/agentihooks-swarm-nats-controller: wrote credentials (2 keys total)",
        "swarm-github-webhook: OpenBao secret/k8s/agentihooks-swarm-nats-github-webhook: wrote credentials (1 keys total)",
        "swarm-ledger: OpenBao secret/k8s/agentihooks-swarm-nats-ledger: wrote credentials (1 keys total)",
    ]
    granted = {}
    for entry in USERS:
        jwt, user = nkey.parse_creds(records[entry["openbao"]]["credentials"])
        claims = nkey.claims(jwt)
        assert user == nkey.derive(account, nkey.USER, f"service-{entry['user']}")
        assert (claims["iss"], claims["sub"], claims["name"]) == (account.public, user.public, entry["user"])
        granted[entry["user"]] = (claims["nats"]["pub"], claims["nats"]["sub"])
    assert granted == {
        "swarm-controller": ({}, {}),
        "swarm-github-webhook": ({"allow": WEBHOOK_PUBLISH}, {"allow": ["_INBOX.>"]}),
        "swarm-ledger": ({"allow": ["swarm.>"]}, {"allow": ["swarm.>"], "deny": ["swarm.x"]}),
    }


def test_a_signed_service_user_is_stable_across_runs(bao):
    client, records = bao
    nats_trust.bootstrap(client, USERS, URL)
    first = nkey.parse_creds(records[USERS[0]["openbao"]]["credentials"])[1]
    nats_trust.bootstrap(client, USERS, URL)
    assert nkey.parse_creds(records[USERS[0]["openbao"]]["credentials"])[1] == first


def test_the_trust_values_preload_the_system_and_swarm_accounts(bao):
    client, records = bao
    values, _ = nats_trust.bootstrap(client, USERS, URL)
    record = records[nats_trust.SIGNER_PATH]
    operator, system, account = (nkey.from_seed(record[f"{name}_seed"]) for name in ("operator", "system", "account"))
    merge = values["config"]["merge"]

    assert list(values) == ["config"] and list(values["config"]) == ["merge"]
    assert list(merge) == ["operator", "system_account", "resolver_preload"]
    assert merge["system_account"] == system.public
    operator_claims = nkey.claims(merge["operator"])
    assert (operator_claims["iss"], operator_claims["sub"], operator_claims["name"]) == (
        operator.public,
        operator.public,
        "swarm",
    )
    assert operator_claims["nats"] == {"type": "operator", "version": 2, "system_account": system.public}
    preload = merge["resolver_preload"]
    assert list(preload) == [system.public, account.public]
    system_claims = nkey.claims(preload[system.public])
    assert (system_claims["iss"], system_claims["sub"], system_claims["name"]) == (
        operator.public,
        system.public,
        "SYS",
    )
    assert system_claims["nats"] == {"type": "account", "version": 2, "limits": nats_trust.SYSTEM_LIMITS}
    assert nats_trust.SYSTEM_LIMITS == {
        "subs": -1,
        "data": -1,
        "payload": -1,
        "imports": -1,
        "exports": -1,
        "conn": -1,
        "leaf": -1,
    }
    account_claims = nkey.claims(preload[account.public])
    assert (account_claims["iss"], account_claims["sub"], account_claims["name"]) == (
        operator.public,
        account.public,
        "SWARM",
    )
    assert "revocations" not in account_claims["nats"]


def test_a_broken_signer_record_is_refused(bao):
    client, records = bao
    records[nats_trust.SIGNER_PATH] = {"operator_seed": "nope"}
    with pytest.raises(auth.HiveError, match="^the NATS signer record agentihooks/hub/nats-signer cannot be used \\("):
        nats_trust.bootstrap(client, USERS, URL)


def test_the_users_file_is_read(tmp_path):
    path = tmp_path / "users.yaml"
    path.write_text(yaml.safe_dump({"users": USERS}))
    assert nats_trust.load_users(path) == USERS


@pytest.mark.parametrize("body", ["", "users: []\n", "users: {}\n", "other: 1\n"])
def test_a_users_file_without_users_is_refused(tmp_path, body):
    path = tmp_path / "users.yaml"
    path.write_text(body)
    with pytest.raises(auth.HiveError) as refused:
        nats_trust.load_users(path)
    assert str(refused.value) == f"{path} must list the service users under users"


@pytest.mark.parametrize(
    "entry", ["swarm-ledger", {"user": "swarm-ledger"}, {"openbao": "k8s/x"}, {"user": "", "openbao": "k8s/x"}]
)
def test_a_user_without_a_name_or_record_is_refused(tmp_path, entry):
    path = tmp_path / "users.yaml"
    path.write_text(yaml.safe_dump({"users": [USERS[0], entry]}))
    with pytest.raises(auth.HiveError) as refused:
        nats_trust.load_users(path)
    assert str(refused.value) == (
        f"every user in {path} needs a user name and the openbao record its credential goes to"
    )


def test_cli_writes_the_trust_values_and_reports_each_user(bao, tmp_path, monkeypatch, capsys):
    _, records = bao
    users, values = tmp_path / "users.yaml", tmp_path / "trust.yaml"
    users.write_text(yaml.safe_dump({"users": USERS[:1]}))
    monkeypatch.setenv("OPENBAO_URL", "https://bao.example")
    monkeypatch.setenv("OPENBAO_TOKEN", "t")

    status = cli.main(["nats-trust", "--users", str(users), "--url", URL, "--values", str(values)])

    out = capsys.readouterr().out.splitlines()
    account = nkey.from_seed(records[nats_trust.SIGNER_PATH]["account_seed"])
    written = yaml.safe_load(values.read_text())
    assert status == 0
    assert out[0].endswith(f"swarm account {account.public}")
    assert out[1:] == [
        "swarm-controller: OpenBao secret/k8s/agentihooks-swarm-nats-controller: wrote credentials (2 keys total)",
        f"wrote the public operator trust to {values}",
    ]
    assert list(written["config"]["merge"]["resolver_preload"])[1] == account.public
    assert values.read_text().count("\n") == 7


def test_cli_refuses_without_openbao(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("OPENBAO_URL", raising=False)
    monkeypatch.delenv("OPENBAO_TOKEN", raising=False)
    status = cli.main(["nats-trust", "--users", "u", "--url", URL, "--values", str(tmp_path / "v")])
    assert status == 1
    assert capsys.readouterr().err == (
        "hive nats-trust refused: nats-trust writes OpenBao, so OPENBAO_URL and OPENBAO_TOKEN must be set\n"
    )


def test_cli_help_names_the_trust_command_and_its_options(monkeypatch, capsys):
    monkeypatch.setenv("COLUMNS", "300")
    top = cli._parser().format_help()
    with pytest.raises(SystemExit):
        cli.main(["nats-trust", "--help"])
    trust = capsys.readouterr().out

    assert " Create the swarm NATS operator trust once and sign the service users of a users file\n" in top
    for text in (
        "YAML file listing each service user and its OpenBao record",
        "NATS address the hive join endpoint pushes account updates to",
        "Helm values file to write the public operator trust to",
    ):
        assert f" {text}\n" in trust
    with pytest.raises(SystemExit):
        cli._parser().parse_args(["nats-trust", "--users", "u", "--url", "x"])


def _opens(url: str, credentials: str | None) -> bool:
    from scripts.events import nats

    try:
        bus = nats.open_bus(BusConfig(backend="nats", url=url, credentials=credentials, provision=False))
    except Exception as exc:
        assert "authorization" in str(exc).lower()
        return False
    bus.close()
    return True


def test_a_server_run_from_the_trust_values_admits_service_users_and_minted_members(bao, tmp_path):
    client, records = bao
    port = nats_server.free_port()
    values, _ = nats_trust.bootstrap(client, USERS, f"nats://127.0.0.1:{port}")
    merge = values["config"]["merge"]
    preload = "".join(f"  {key}: {jwt}\n" for key, jwt in merge["resolver_preload"].items())
    config = tmp_path / "nats.conf"
    config.write_text(
        f"host: 127.0.0.1\nport: {port}\nmax_control_line: 65536\n"
        f"operator: {merge['operator']}\nsystem_account: {merge['system_account']}\n"
        f'jetstream: {{store_dir: "{tmp_path / "js"}"}}\n'
        f'resolver: {{type: full, dir: "{tmp_path / "jwt"}"}}\n'
        f"resolver_preload: {{\n{preload}}}\n"
    )
    process = subprocess.Popen(
        [nats_server.binary(), "-c", str(config)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        nats_server.wait_listening(port, process)
        url = f"nats://127.0.0.1:{port}"
        signer = _signer(records)
        assert _opens(url, records[USERS[0]["openbao"]]["credentials"])
        assert _opens(url, nats_grant.mint(signer, "h1"))
        assert not _opens(url, None)
        nats_grant.push(signer, nats_grant.account_jwt(signer, {}))
    finally:
        process.terminate()
        process.wait(10)
