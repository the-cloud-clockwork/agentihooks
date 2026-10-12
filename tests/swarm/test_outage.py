import pytest

from scripts.swarm import outage

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("kind", ["redis", "ledger", "provider"])
def test_outage_record_survives_repeated_checks_and_closes_on_recovery(tmp_path, monkeypatch, kind):
    monkeypatch.setattr(outage, "HOME", tmp_path)
    assert outage.read("sw") == {}
    outage.observe("sw", kind, ["connection refused"], 1000)
    outage.observe("sw", kind, ["still unavailable"], 2000)
    assert outage.read("sw")[kind] == {
        "kind": kind,
        "started_at": 1000,
        "evidence": ["still unavailable"],
        "ended_at": None,
    }
    assert outage.open_records("sw") == list(outage.read("sw").values())
    outage.observe("sw", kind, [], 3000)
    assert outage.read("sw")[kind]["ended_at"] == 3000
    assert outage.open_records("sw") == []
    outage.observe("sw", kind, [], 4000)
    assert outage.read("sw")[kind]["ended_at"] == 3000
    outage.observe("sw", kind, ["unavailable again"], 5000)
    assert outage.read("sw")[kind]["started_at"] == 5000
    assert outage.read("sw")[kind]["ended_at"] is None
    assert (tmp_path / "sw" / "outages.json").is_file()
