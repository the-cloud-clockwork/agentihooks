import json
import shutil
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

import scripts.swarm_v2.architecture as architecture
from scripts.swarm_v2.runtime.commands import Principal, Role

ROOT = Path(__file__).resolve().parents[1]
RECORD = ROOT / "docs" / "swarm-v2" / "architecture.json"
FIXTURES = Path(__file__).parent / "fixtures" / "swarm_v2" / "architecture"
SIGNER = Ed25519PrivateKey.from_private_bytes(b"k" * 32)
KEY = SIGNER.public_key()
OTHER_KEY = Ed25519PrivateKey.from_private_bytes(b"o" * 32).public_key()
PUBLIC_HEX = KEY.public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
SLUG = "rig"
DISPATCHER_REASON = "inserts another coding-task queue beside Swarm reconciliation controller (AD-05)"
PRINCIPALS = {
    "operator-credential": Principal("nestor", Role.OPERATOR),
    "master-credential": Principal("master@rig-1", Role.MASTER, "exe-1", 1),
}


def _authenticate(slug, credential):
    return PRINCIPALS.get(credential) if slug == SLUG else None


def _record(tmp_path):
    path = tmp_path / "architecture.json"
    shutil.copy(RECORD, path)
    return path


def _inventory():
    return architecture.load_inventory(FIXTURES / "inventory.json")


def _dispatcher():
    return _inventory()["proposals"][0]


def _approve(path, proposal, credential="operator-credential", slug=SLUG, authenticate=_authenticate):
    return architecture.approve(
        path,
        proposal,
        "operator moves dispatch",
        slug=slug,
        credential=credential,
        authenticate=authenticate,
        signer=SIGNER,
    )


def test_an_authenticated_operator_approval_admits_its_dispatcher(tmp_path):
    path = _record(tmp_path)
    change = _approve(path, _dispatcher())
    assert change["proposal"] == "duplicate-dispatcher"
    assert change["sha256"] == architecture.digest(_dispatcher())
    assert change["approved_by"] == "nestor"
    assert change["revision"] == 1
    assert change["key_id"] == architecture.key_id(KEY) != architecture.key_id(OTHER_KEY)
    assert len(change["key_id"]) == 16
    assert architecture.load_record(path)["operator_changes"] == [change]
    result = architecture.apply_inventory(path, _inventory(), key=KEY)
    assert result["accepted"] == ["duplicate-dispatcher", "embedding-backlog"]
    after = architecture.load_record(path)
    assert architecture.authorities(after, KEY) == ["Swarm reconciliation controller"]
    assert architecture.check(after, KEY) == []


def test_a_forged_operator_label_is_refused(tmp_path):
    path = _record(tmp_path)
    record = architecture.load_record(path)
    dispatcher = _dispatcher()
    record["operator_changes"] = [
        {
            "proposal": dispatcher["id"],
            "sha256": architecture.digest(dispatcher),
            "approved_by": "operator",
            "revision": 1,
            "reason": "forged",
        }
    ]
    path.write_text(json.dumps(record))
    assert architecture.approved(record, dispatcher, KEY) is False
    result = architecture.apply_inventory(path, _inventory(), key=KEY)
    assert result["accepted"] == ["embedding-backlog"]
    assert result["rejected"][0]["reason"] == DISPATCHER_REASON


@pytest.mark.parametrize(
    "tamper",
    [
        {"signature": "0" * 64},
        {"approved_by": "operator"},
        {"reason": "another reason"},
        {"revision": 2},
        {"proposal": "other"},
        {"key_id": "0" * 16},
        {"signature": None},
        {"signature": "zz"},
        {"signature": "UPPER"},
    ],
)
def test_a_signed_change_with_any_edited_field_is_refused(tmp_path, tamper):
    path = _record(tmp_path)
    change = _approve(path, _dispatcher())
    record = architecture.load_record(path)
    assert architecture.approved(record, _dispatcher(), KEY) is True
    record["operator_changes"] = [{**change, **tamper}]
    assert architecture.approved(record, _dispatcher(), KEY) is False


def test_a_signed_change_admits_nothing_without_the_verify_key(tmp_path):
    path = _record(tmp_path)
    _approve(path, _dispatcher())
    record = architecture.load_record(path)
    assert architecture.approved(record, _dispatcher(), None) is False
    assert architecture.approved(record, _dispatcher(), OTHER_KEY) is False
    assert architecture.approved(record, _dispatcher()) is False
    result = architecture.apply_inventory(path, _inventory())
    assert result["rejected"][0]["reason"] == DISPATCHER_REASON


def test_a_signed_change_covers_only_its_exact_content(tmp_path):
    path = _record(tmp_path)
    _approve(path, _dispatcher())
    record = architecture.load_record(path)
    assert architecture.approved(record, {**_dispatcher(), "name": "Unrelated second dispatcher"}, KEY) is False
    assert architecture.approved(record, {**_dispatcher(), "id": "other"}, KEY) is False


@pytest.mark.parametrize(
    ("credential", "slug", "authenticate"),
    [
        ("master-credential", SLUG, _authenticate),
        ("unknown", SLUG, _authenticate),
        ("operator-credential", "other-swarm", _authenticate),
        ("operator-credential", SLUG, lambda slug, credential: {"name": "nestor", "role": "operator"}),
        ("operator-credential", SLUG, lambda slug, credential: Principal("", Role.OPERATOR)),
    ],
)
def test_an_approval_outside_the_authenticated_operator_transport_writes_nothing(
    tmp_path, credential, slug, authenticate
):
    path = _record(tmp_path)
    before = path.read_text()
    with pytest.raises(architecture.ArchitectureError, match="authenticated operator required"):
        _approve(path, _dispatcher(), credential=credential, slug=slug, authenticate=authenticate)
    assert path.read_text() == before


def test_approving_the_same_content_twice_keeps_one_change(tmp_path):
    path = _record(tmp_path)
    first = _approve(path, _dispatcher())
    assert _approve(path, _dispatcher()) == first
    assert architecture.load_record(path)["operator_changes"] == [first]


def test_a_forged_change_never_removes_a_recorded_authority(tmp_path):
    record = architecture.load_record(RECORD)
    controller = next(c for c in record["components"] if architecture.dispatches(c))
    forged = {**controller, "name": "Second dispatcher", "proposal": "second", "authoritative_state": "second queue"}
    record["components"].append(forged)
    record["operator_changes"] = [
        {"proposal": "second", "sha256": "x", "approved_by": "operator", "revision": 1, "reason": "forged"}
    ]
    assert architecture.authorities(record, KEY) == [controller["name"], "Second dispatcher"]
    assert architecture.check(record, KEY) == ["expected one coding-task authority, found 2"]


def test_a_label_copy_of_an_authentic_approval_keeps_the_dispatcher_an_authority(tmp_path):
    path = _record(tmp_path)
    change = _approve(path, _dispatcher())
    architecture.apply_inventory(path, _inventory(), key=KEY)
    record = architecture.load_record(path)
    record["operator_changes"] = [{k: v for k, v in change.items() if k not in ("signature", "key_id")}]
    assert architecture.authorities(record, KEY) == ["Swarm reconciliation controller", _dispatcher()["name"]]
    assert architecture.check(record, KEY) == ["expected one coding-task authority, found 2"]


@pytest.mark.parametrize("signature", ["é" * 64, "\ud800"])
def test_a_non_ascii_signature_is_refused_without_an_error(tmp_path, signature):
    path = _record(tmp_path)
    change = _approve(path, _dispatcher())
    record = architecture.load_record(path)
    record["operator_changes"] = [{**change, "signature": signature}]
    assert architecture.approved(record, _dispatcher(), KEY) is False


def test_a_principal_with_a_non_string_name_writes_nothing(tmp_path):
    path = _record(tmp_path)
    before = path.read_text()
    with pytest.raises(architecture.ArchitectureError, match="authenticated operator required"):
        _approve(path, _dispatcher(), authenticate=lambda slug, credential: Principal(7, Role.OPERATOR))
    assert path.read_text() == before


def test_render_lists_only_authentic_approvals(tmp_path):
    path = _record(tmp_path)
    _approve(path, _dispatcher())
    record = architecture.load_record(path)
    record["operator_changes"].append({**record["operator_changes"][0], "proposal": "forged", "signature": "x"})
    line = "\nOperator architecture changes: duplicate-dispatcher by nestor at revision 1 (operator moves dispatch).\n"
    assert line in architecture.render(record, KEY)
    assert "\nOperator architecture changes: none.\n" in architecture.render(record)


@pytest.mark.parametrize("environ", [{}, {"SWARM_ARCHITECTURE_PUBLIC_KEY": ""}])
def test_no_verify_key_without_the_variable(environ):
    assert architecture.verify_key(environ) is None


def test_the_verify_key_is_the_public_key_from_the_environment():
    key = architecture.verify_key({"SWARM_ARCHITECTURE_PUBLIC_KEY": PUBLIC_HEX})
    assert architecture.key_id(key) == architecture.key_id(KEY)


def test_the_public_key_cannot_sign():
    assert not hasattr(architecture.verify_key({"SWARM_ARCHITECTURE_PUBLIC_KEY": PUBLIC_HEX}), "sign")


@pytest.mark.parametrize("raw", ["short", "ab" * 31, "zz" * 32])
def test_a_malformed_public_key_is_refused_by_name(raw):
    with pytest.raises(architecture.ArchitectureError) as caught:
        architecture.verify_key({"SWARM_ARCHITECTURE_PUBLIC_KEY": raw})
    assert str(caught.value) == "SWARM_ARCHITECTURE_PUBLIC_KEY must be a hex Ed25519 public key"


def test_the_cli_counts_an_approval_only_with_the_verify_key(tmp_path, monkeypatch, capsys):
    path = _record(tmp_path)
    markdown = tmp_path / "decisions.md"
    _approve(path, _dispatcher())
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps(_inventory()))
    monkeypatch.setenv("SWARM_ARCHITECTURE_PUBLIC_KEY", PUBLIC_HEX)
    args = ["--record", str(path), "--inventory", str(inventory), "--markdown", str(markdown)]
    assert architecture.main(["record", *args]) == 0
    assert json.loads(capsys.readouterr().out)["accepted"] == ["duplicate-dispatcher", "embedding-backlog"]
    assert "duplicate-dispatcher by nestor" in markdown.read_text()
    assert architecture.main(["check", "--record", str(path)]) == 0
    assert capsys.readouterr().out == "ok\n"
    monkeypatch.delenv("SWARM_ARCHITECTURE_PUBLIC_KEY")
    assert architecture.main(["check", "--record", str(path)]) == 1
    assert capsys.readouterr().out == "expected one coding-task authority, found 2\n"


def test_the_cli_reports_a_malformed_public_key_without_a_traceback(monkeypatch, capsys):
    monkeypatch.setenv("SWARM_ARCHITECTURE_PUBLIC_KEY", "short")
    assert architecture.main(["check", "--record", str(RECORD)]) == 2
    assert capsys.readouterr().err == "error: SWARM_ARCHITECTURE_PUBLIC_KEY must be a hex Ed25519 public key\n"


SIGNING_HEX = (b"k" * 32).hex()
REFUSED = "error: authenticated operator required for an architecture change\n"


def _rollback(path, credential="operator-credential", to=1, operation="r"):
    return architecture.rollback(path, to, operation, slug=SLUG, credential=credential, authenticate=_authenticate)


def _operator_cli(tmp_path, monkeypatch, *, agent=None, signing=SIGNING_HEX):
    path = _record(tmp_path)
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps(_inventory()))
    monkeypatch.setattr(architecture, "_page_credential", {SLUG: "page-credential"}.get)
    monkeypatch.delenv("SWARM_ARCHITECTURE_PUBLIC_KEY", raising=False)
    if signing is None:
        monkeypatch.delenv("SWARM_ARCHITECTURE_SIGNING_KEY", raising=False)
    else:
        monkeypatch.setenv("SWARM_ARCHITECTURE_SIGNING_KEY", signing)
    if agent is None:
        monkeypatch.delenv("AGENTIHOOKS_SWARM", raising=False)
    else:
        monkeypatch.setenv("AGENTIHOOKS_SWARM", SLUG)
        monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", agent)
    markdown = tmp_path / "decisions.md"
    files = ["--record", str(path), "--markdown", str(markdown)]
    approve = ["approve", "--slug", SLUG, "--inventory", str(inventory), *files]
    return path, markdown, [*approve, "--proposal", "duplicate-dispatcher", "--reason", "operator moves dispatch"]


def test_the_cli_approves_for_the_operator_through_the_page_credential(tmp_path, monkeypatch, capsys):
    path, markdown, argv = _operator_cli(tmp_path, monkeypatch)
    assert architecture.main(argv) == 0
    change = json.loads(capsys.readouterr().out)
    assert architecture.load_record(path)["operator_changes"] == [change]
    assert change["approved_by"] == "operator"
    assert change["key_id"] == architecture.key_id(KEY)
    assert architecture.approved(architecture.load_record(path), _dispatcher(), KEY)
    assert markdown.read_text() == architecture.render(architecture.load_record(path), KEY)
    assert "duplicate-dispatcher by operator at revision 1 (operator moves dispatch)" in markdown.read_text()


@pytest.mark.parametrize("agent", ["engineer@rig-1", "master@rig-1"])
def test_the_cli_refuses_an_approval_from_any_agent(tmp_path, monkeypatch, capsys, agent):
    path, markdown, argv = _operator_cli(tmp_path, monkeypatch, agent=agent)
    before = path.read_bytes()
    assert architecture.main(argv) == 2
    assert capsys.readouterr().err == REFUSED
    assert path.read_bytes() == before
    assert not markdown.exists()


def test_the_cli_refuses_an_approval_for_another_ledger(tmp_path, monkeypatch, capsys):
    path, _, argv = _operator_cli(tmp_path, monkeypatch)
    before = path.read_bytes()
    argv[argv.index("--slug") + 1] = "other"
    assert architecture.main(argv) == 2
    assert capsys.readouterr().err == REFUSED
    assert path.read_bytes() == before


def test_the_cli_names_a_proposal_missing_from_the_inventory(tmp_path, monkeypatch, capsys):
    path, _, argv = _operator_cli(tmp_path, monkeypatch)
    before = path.read_bytes()
    argv[argv.index("--proposal") + 1] = "nope"
    inventory = argv[argv.index("--inventory") + 1]
    assert architecture.main(argv) == 2
    assert capsys.readouterr().err == f"error: {inventory} has no proposal nope\n"
    assert path.read_bytes() == before


@pytest.mark.parametrize("signing", [None, "", "short", "zz" * 32, SIGNING_HEX + "00"])
def test_the_cli_approve_needs_a_hex_signing_key(tmp_path, monkeypatch, capsys, signing):
    path, _, argv = _operator_cli(tmp_path, monkeypatch, signing=signing)
    before = path.read_bytes()
    assert architecture.main(argv) == 2
    assert capsys.readouterr().err == "error: SWARM_ARCHITECTURE_SIGNING_KEY must be a hex Ed25519 private key\n"
    assert path.read_bytes() == before


def test_the_cli_refuses_a_signing_key_the_verify_key_would_not_count(tmp_path, monkeypatch, capsys):
    path, _, argv = _operator_cli(tmp_path, monkeypatch)
    before = path.read_bytes()
    other = Ed25519PrivateKey.from_private_bytes(b"o" * 32).public_key()
    monkeypatch.setenv("SWARM_ARCHITECTURE_PUBLIC_KEY", other.public_bytes(Encoding.Raw, PublicFormat.Raw).hex())
    assert architecture.main(argv) == 2
    assert capsys.readouterr().err == (
        "error: SWARM_ARCHITECTURE_SIGNING_KEY does not match SWARM_ARCHITECTURE_PUBLIC_KEY\n"
    )
    assert path.read_bytes() == before


def test_the_cli_approves_with_a_matching_verify_key(tmp_path, monkeypatch, capsys):
    path, markdown, argv = _operator_cli(tmp_path, monkeypatch)
    monkeypatch.setenv("SWARM_ARCHITECTURE_PUBLIC_KEY", PUBLIC_HEX)
    assert architecture.main(argv) == 0
    assert architecture.approved(architecture.load_record(path), _dispatcher(), KEY)
    assert "duplicate-dispatcher by operator" in markdown.read_text()


def test_the_signing_key_is_the_private_key_from_the_environment():
    signer = architecture.signing_key({"SWARM_ARCHITECTURE_SIGNING_KEY": SIGNING_HEX})
    assert architecture.key_id(signer.public_key()) == architecture.key_id(KEY)


def test_an_operator_rollback_drops_an_approved_dispatcher(tmp_path):
    path = _record(tmp_path)
    _approve(path, _dispatcher())
    architecture.apply_inventory(path, _inventory(), key=KEY)
    result = _rollback(path)
    assert result["rolled_back"] == ["Kubernetes task dispatcher", "Brain arc embedding backlog"]
    assert architecture.load_record(path)["components"] == architecture.load_record(RECORD)["components"]


@pytest.mark.parametrize("credential", ["master-credential", "unknown", ""])
def test_a_rollback_without_the_operator_writes_nothing(tmp_path, credential):
    path = _record(tmp_path)
    _approve(path, _dispatcher())
    architecture.apply_inventory(path, _inventory(), key=KEY)
    before = path.read_bytes()
    with pytest.raises(architecture.ArchitectureError) as caught:
        _rollback(path, credential)
    assert str(caught.value) == "authenticated operator required for an architecture change"
    assert path.read_bytes() == before
    assert "Kubernetes task dispatcher" in [c["name"] for c in architecture.load_record(path)["components"]]


def test_a_replayed_rollback_is_refused_without_the_operator(tmp_path):
    path = _record(tmp_path)
    architecture.apply_inventory(path, _inventory())
    _rollback(path)
    with pytest.raises(architecture.ArchitectureError) as caught:
        _rollback(path, "master-credential")
    assert str(caught.value) == "authenticated operator required for an architecture change"


@pytest.mark.parametrize(
    "authenticate",
    [
        lambda slug, credential: {"name": "nestor", "role": "operator"},
        lambda slug, credential: Principal("", Role.OPERATOR),
        lambda slug, credential: Principal(7, Role.OPERATOR),
    ],
)
def test_a_rollback_needs_a_named_operator_principal(tmp_path, authenticate):
    path = _record(tmp_path)
    architecture.apply_inventory(path, _inventory())
    before = path.read_bytes()
    with pytest.raises(architecture.ArchitectureError):
        architecture.rollback(path, 1, "r", slug=SLUG, credential="operator-credential", authenticate=authenticate)
    assert path.read_bytes() == before


def test_the_cli_rolls_back_for_the_operator_and_refuses_an_agent(tmp_path, monkeypatch, capsys):
    path, markdown, _ = _operator_cli(tmp_path, monkeypatch, agent="master@rig-1")
    architecture.apply_inventory(path, _inventory())
    before = path.read_bytes()
    argv = ["rollback", "--slug", SLUG, "--record", str(path), "--markdown", str(markdown), "--to", "1"]
    argv += ["--operation", "r"]
    assert architecture.main(argv) == 2
    assert capsys.readouterr().err == REFUSED
    assert path.read_bytes() == before
    monkeypatch.delenv("AGENTIHOOKS_SWARM")
    assert architecture.main(argv) == 0
    assert json.loads(capsys.readouterr().out)["rolled_back"] == ["Brain arc embedding backlog"]
    assert markdown.read_text() == architecture.render(architecture.load_record(path))
