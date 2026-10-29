import argparse
import json

import pytest

from scripts.swarm import cli, outage, status
from scripts.swarm.store import SwarmError
from tests.swarm.test_tick import store as store

pytestmark = pytest.mark.unit


def test_status_report_carries_only_open_outages_with_their_evidence(store, tmp_path, monkeypatch):
    monkeypatch.setattr(outage, "HOME", tmp_path)
    outage.observe("sw", "redis", ["Redis connect check failed"], 1000)
    outage.observe("sw", "ledger", ["Ledger health probe failed"], 2000)
    outage.observe("sw", "ledger", [], 3000)
    monkeypatch.setattr(status, "page_quota", lambda: {})
    assert status.status_report(store, "sw", {"tasks": []})["outages"] == [
        {
            "kind": "redis",
            "started_at": 1000,
            "evidence": ["Redis connect check failed"],
            "ended_at": None,
        }
    ]


def test_text_status_lists_each_open_outage_even_when_ledger_reads_fail(store, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(outage, "HOME", tmp_path)
    outage.observe("sw", "provider", ["worker (claude): API Error: 529"], 1000)

    def unavailable(*args):
        raise SwarmError("ledger unavailable")

    monkeypatch.setattr(cli.LedgerClient, "state", unavailable)
    with pytest.raises(SwarmError):
        cli.cmd_status(store, argparse.Namespace(slug="sw", json=False))
    assert capsys.readouterr().out == "outage provider since 1000: worker (claude): API Error: 529\n"


@pytest.mark.parametrize("as_json", [False, True])
def test_status_shows_local_outage_evidence_while_redis_cannot_connect(tmp_path, monkeypatch, capsys, as_json):
    monkeypatch.setattr(outage, "HOME", tmp_path)
    outage.observe("sw", "redis", ["Redis connect check failed"], 1000)

    def unavailable():
        raise SwarmError("Redis is unreachable")

    monkeypatch.setattr(cli, "connect", unavailable)
    argv = ["sw", "status", *(["--json"] if as_json else [])]
    assert cli.main(argv) == 0
    output = capsys.readouterr().out
    if as_json:
        assert json.loads(output)["outages"][0]["evidence"] == ["Redis connect check failed"]
    else:
        assert "Redis connect check failed" in output
