import json
import shutil
from pathlib import Path

import pytest

import scripts.swarm_v2.architecture as architecture
from scripts.swarm_v2.auth_context import LaunchKey
from scripts.swarm_v2.runtime.commands import Principal, Role

ROOT = Path(__file__).resolve().parents[1]
RECORD = ROOT / "docs" / "swarm-v2" / "architecture.json"
FIXTURES = Path(__file__).parent / "fixtures" / "swarm_v2" / "architecture"
KEY = LaunchKey("architecture-1", b"k" * 32)
OTHER_KEY = LaunchKey("architecture-2", b"o" * 32)
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
        path, proposal, "operator moves dispatch", slug=slug, credential=credential, authenticate=authenticate, key=KEY
    )


def test_an_authenticated_operator_approval_admits_its_dispatcher(tmp_path):
    path = _record(tmp_path)
    change = _approve(path, _dispatcher())
    assert change["proposal"] == "duplicate-dispatcher"
    assert change["sha256"] == architecture.digest(_dispatcher())
    assert change["approved_by"] == "nestor"
    assert change["revision"] == 1
    assert change["key_id"] == "architecture-1"
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
        {"key_id": "architecture-2"},
    ],
)
def test_a_signed_change_with_any_edited_field_is_refused(tmp_path, tamper):
    path = _record(tmp_path)
    change = _approve(path, _dispatcher())
    record = architecture.load_record(path)
    assert architecture.approved(record, _dispatcher(), KEY) is True
    record["operator_changes"] = [{**change, **tamper}]
    assert architecture.approved(record, _dispatcher(), KEY) is False


def test_a_signed_change_admits_nothing_without_the_signing_key(tmp_path):
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
