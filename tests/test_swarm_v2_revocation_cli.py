"""Tests for the SV2-RET-02 revocation command line."""

import json

import pytest

from hooks.memory import revocation
from hooks.memory.catalog import Catalog, SessionSource
from scripts.swarm_v2 import revocation as cli

pytestmark = pytest.mark.unit
DOMAIN = "corpus-a"


@pytest.fixture
def path(tmp_path):
    return str(tmp_path / "catalog.sqlite3")


@pytest.fixture
def catalog(path):
    found = Catalog(path)
    found.register(SessionSource(DOMAIN, "s1", "g", "claude", "origin", "retain-until-deleted"))
    yield found
    found.close()


def test_cli_registers_derived_material_and_propagates_a_revocation(catalog, path, tmp_path, capsys):
    document = tmp_path / "derived.json"
    packs = [{"pack_id": "p1", "body": {"previews": ["x"]}, "sessions": ["s1"]}]
    document.write_text(json.dumps({"packs": packs, "lessons": [{"lesson_id": "l1", "text": "t", "support": ["s1"]}]}))
    assert cli.main(["register", path, DOMAIN, "--document", str(document)]) == 0
    assert json.loads(capsys.readouterr().out) == {"packs": 1, "lessons": 1}
    granted, propagator = revocation.Grant("p", DOMAIN, frozenset()), revocation.Propagator(catalog)
    assert propagator.pack(granted, DOMAIN, "p1") == {"previews": ["x"]}
    assert propagator.lesson(granted, DOMAIN, "l1") == {
        "lesson_id": "l1",
        "state": "inferred",
        "text": "t",
        "support": ["s1"],
    }
    assert cli.main(["exposures", path, DOMAIN, "--session", "s1"]) == 0
    assert json.loads(capsys.readouterr().out) == {"exposures": {"context_pack": 1, "derived_lesson": 1}}
    catalog.set_state(DOMAIN, "s1", "deleted", 1, 10.0)
    assert cli.main(["catch-up", path, DOMAIN], clock=lambda: 12.0) == 0
    notices = json.loads(capsys.readouterr().out)["notices"]
    assert [(item["consumer"], item["references"]) for item in notices] == [
        ("search", []),
        ("packs", ["p1"]),
        ("lessons", ["l1"]),
    ]
    assert cli.main(["lags", path, DOMAIN]) == 0
    assert json.loads(capsys.readouterr().out)["lags"][0] == {
        "consumer": "lessons",
        "sequence": 1,
        "session_uid": "s1",
        revocation.METRIC: 2.0,
    }
    assert cli.main(["exposures", path, DOMAIN, "--session", "s1"]) == 0
    assert json.loads(capsys.readouterr().out) == {"exposures": {}}


def test_cli_register_takes_an_empty_document(path, tmp_path, capsys):
    document = tmp_path / "derived.json"
    document.write_text("{}")
    assert cli.main(["register", path, DOMAIN, "--document", str(document)]) == 0
    assert json.loads(capsys.readouterr().out) == {"packs": 0, "lessons": 0}


def test_cli_catch_up_reads_the_wall_clock_by_default(catalog, path, capsys, monkeypatch):
    catalog.set_state(DOMAIN, "s1", "deleted", 1, 10.0)
    monkeypatch.setattr(cli.time, "time", lambda: 15.0)
    cli.main(["catch-up", path, DOMAIN])
    capsys.readouterr()
    cli.main(["lags", path, DOMAIN])
    assert json.loads(capsys.readouterr().out)["lags"][0][revocation.METRIC] == 5.0


@pytest.mark.parametrize(
    "argv,message",
    [
        (["exposures"], "--session is given with exposures and only with it"),
        (["lags", "--session", "s1"], "--session is given with exposures and only with it"),
        (["register"], "--document is given with register and only with it"),
        (["lags", "--document", "d"], "--document is given with register and only with it"),
    ],
)
def test_cli_refuses_options_for_the_wrong_command(argv, message, tmp_path, capsys):
    target = tmp_path / "c.sqlite3"
    with pytest.raises(SystemExit) as raised:
        cli.main([argv[0], str(target), DOMAIN, *argv[1:]])
    assert raised.value.code == 2
    assert capsys.readouterr().err.splitlines()[-1].endswith(f"error: {message}")
    assert not target.exists()


def test_cli_refuses_an_unknown_command(tmp_path):
    with pytest.raises(SystemExit) as raised:
        cli.main(["purge", str(tmp_path / "c.sqlite3"), DOMAIN])
    assert raised.value.code == 2
