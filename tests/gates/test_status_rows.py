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


def _a_deny_among_thirty_count_rows():
    log.append("sw", log.Row.of("subagents", "count", WHO, "Agent", "sub agent call", now_ms=0))
    log.append("sw", log.Row.of("talk", "deny", WHO, "Bash", "over budget", now_ms=1))
    for at in range(2, 31):
        log.append("sw", log.Row.of("reruns", "count", WHO, "Bash", "ci rerun", now_ms=at))


def test_status_report_lists_only_decision_rows_and_keeps_count_rows_in_the_log():
    _a_deny_among_thirty_count_rows()
    log.append("sw", log.Row.of("talk", "observe", WHO, "Bash", "would deny", now_ms=31))
    log.append("sw", log.Row.of("talk", "lift", WHO, reason="lifted", now_ms=32))
    log.append("sw", log.Row.of("talk", "fail-open", WHO, "Bash", "crashed", now_ms=33))
    gates = status.status_report(saved(), "sw", {"tasks": []})["gates"]
    assert [(r["kind"], r["at"]) for r in gates] == [("deny", 1), ("observe", 31), ("lift", 32), ("fail-open", 33)]
    assert [r["kind"] for r in log.recent("sw", limit=None)].count("count") == 30


def test_status_text_shows_the_deny_among_thirty_count_rows(monkeypatch, capsys):
    from tests.swarm.test_tick import FakeLedger

    _a_deny_among_thirty_count_rows()
    monkeypatch.setattr(cli, "LedgerClient", lambda: FakeLedger([]))
    cli.cmd_status(saved(), SimpleNamespace(slug="sw", json=False))
    lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("gate ")]
    assert lines == ["gate  deny  talk  engineer@1-1  t1  over budget"]
