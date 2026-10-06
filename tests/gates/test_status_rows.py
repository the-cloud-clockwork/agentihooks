"""swarm status lists the recent gate log rows, in its JSON report and its text."""

from types import SimpleNamespace

import pytest

from scripts.gates import log
from scripts.gates.base import Who
from scripts.swarm import cli, status
from scripts.swarm.store import RedisStore, SwarmConfig

pytestmark = pytest.mark.xdist_group("fakeredis")
WHO = Who(name="engineer@1-1", swarm="sw", task="t1")


def saved():
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/r", 1, 0))
    return store


def test_status_report_carries_the_recent_gate_rows():
    log.append("sw", log.Row.of("talk", "deny", WHO, "Bash", "over budget", now_ms=5))
    assert status.status_report(saved(), "sw", {"tasks": []})["gates"] == [
        {
            "at": 5,
            "gate": "talk",
            "kind": "deny",
            "agent": "engineer@1-1",
            "task": "t1",
            "tool": "Bash",
            "reason": "over budget",
        }
    ]


def test_status_report_without_a_gate_log_is_empty():
    assert status.status_report(saved(), "sw", {"tasks": []})["gates"] == []


def test_status_text_prints_one_line_per_recent_gate_row(monkeypatch, capsys):
    from tests.swarm.test_tick import FakeLedger

    log.append("sw", log.Row.of("talk", "observe", WHO, "Bash", "over budget", now_ms=5))
    monkeypatch.setattr(cli, "LedgerClient", lambda: FakeLedger([]))
    cli.cmd_status(saved(), SimpleNamespace(slug="sw", json=False))
    lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("gate ")]
    assert lines == ["gate  observe  talk  engineer@1-1  t1  over budget"]
