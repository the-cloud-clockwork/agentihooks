"""A gate whose implementation cannot be imported lets the call through, writes a fail-open row naming the gate and the
import error, and raises one setup alert per gate per hour naming the gate and the interpreter."""

import io
import json
import subprocess
import sys
import time
from importlib import import_module
from pathlib import Path

import pytest

from scripts.gates import entry, log
from scripts.gates.base import Decision
from scripts.swarm_ledger import ledger
from scripts.swarm_ledger.api.client import ResourceClient

ROOT = Path(__file__).resolve().parents[2]
SLUG, ME, TASK = "sw", "engineer@1-1", "t1"
ENV = {"AGENTIHOOKS_SWARM": SLUG, "AGENTIHOOKS_AGENT_NAME": ME, "AGENTIHOOKS_SWARM_TASK": TASK}
PAYLOAD = json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}, "session_id": "sid-import"})
START, HOUR = 1_800_000_000.0, 3600
PROCESS = """
import io, json, sys, time
from importlib import import_module

from scripts.gates import entry
from scripts.swarm_ledger import ledger
from scripts.swarm_ledger.api.client import ResourceClient

home, sent, now, gate, module, payload, environ = sys.argv[1:8]
time.time = lambda: float(now)


def transport(slug, path, body):
    if body is None:
        return {"revision": 1}
    with open(sent, "a") as out:
        out.write(json.dumps(body["ops"][0]) + "\\n")
    return {"revision": 2}


ledger.client = lambda slug, timeout=10: ResourceClient("http://ledger.invalid", {}, timeout, transport=transport)
entry.GATES[gate] = (lambda: import_module(module),)
raise SystemExit(entry.main([gate], io.StringIO(payload), json.loads(environ), home))
"""


class Stub:
    def __init__(self, name: str, decision: Decision | Exception) -> None:
        self.name, self.decision, self.default_mode = name, decision, "enforce"

    def matches(self, call: object) -> bool:
        return True

    def decide(self, call: object, who: object, state: object) -> Decision:
        if isinstance(self.decision, Exception):
            raise self.decision
        return self.decision


class World:
    def __init__(self, home: Path) -> None:
        self.home, self.now, self.sent = home, START, []

    def clock(self) -> float:
        return self.now

    def transport(self, slug: str, path: str, body: dict | None) -> dict:
        if body is None:
            return {"revision": 1}
        self.sent.extend((slug, op) for op in body["ops"])
        return {"revision": 2}

    def main(self, *names: str, environ: dict | None = None) -> int:
        return entry.main(list(names), io.StringIO(PAYLOAD), dict(ENV if environ is None else environ), self.home)

    def rows(self) -> list[dict]:
        path = log.gate_log_path(SLUG, self.home)
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def alerts(self) -> list[tuple[str, str, str, str, str]]:
        return [(slug, op["op"], op["by"], op["source"], op["item"]) for slug, op in self.sent]


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    held = World(tmp_path / "swarm")
    monkeypatch.setattr(time, "time", held.clock)
    monkeypatch.setattr(
        ledger, "client", lambda slug, timeout=10: ResourceClient("http://ledger.invalid", {}, timeout, held.transport)
    )
    return held


def missing(module: str) -> tuple:
    return (lambda: import_module(module),)


def summary(rows: list[dict]) -> list[tuple[str, str, str, str, str]]:
    return [(row["gate"], row["kind"], row["agent"], row["task"], row["tool"]) for row in rows]


@pytest.mark.feature("gate-import-failure-alerts")
def test_a_gate_whose_module_is_missing_lets_the_call_through_with_a_row_and_an_alert(world, monkeypatch):
    monkeypatch.setitem(entry.GATES, "absent", missing("scripts.gates.absent_gate_module"))
    assert world.main("absent") == 0
    rows = world.rows()
    assert summary(rows) == [("absent", "fail-open", ME, TASK, "Bash")]
    assert "ModuleNotFoundError: No module named 'scripts.gates.absent_gate_module'" in rows[0]["reason"]
    assert sys.executable in rows[0]["reason"]
    assert world.alerts() == [(SLUG, "alert_raise", ME, "setup", "gate-import-absent")]
    text = world.sent[0][1]["text"]
    assert "absent" in text and sys.executable in text and "absent_gate_module" in text


@pytest.mark.feature("gate-import-failure-alerts")
def test_a_gate_missing_a_transitive_dependency_is_reported_under_its_registered_name(world, monkeypatch, tmp_path):
    (tmp_path / "gate_with_a_lost_dependency.py").write_text("import a_dependency_that_is_not_installed\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setitem(entry.GATES, "reruns", missing("gate_with_a_lost_dependency"))
    assert world.main("reruns") == 0
    rows = world.rows()
    assert summary(rows) == [("reruns", "fail-open", ME, TASK, "Bash")]
    assert "No module named 'a_dependency_that_is_not_installed'" in rows[0]["reason"]
    assert world.alerts() == [(SLUG, "alert_raise", ME, "setup", "gate-import-reruns")]


@pytest.mark.feature("gate-import-failure-alerts")
def test_a_healthy_gate_named_on_the_same_call_still_denies(world, monkeypatch):
    monkeypatch.setitem(entry.GATES, "absent", missing("scripts.gates.absent_gate_module"))
    monkeypatch.setitem(entry.GATES, "stub", Stub("stub", Decision.deny("stub says no")))
    assert world.main("absent", "stub") == 2
    assert [row["kind"] for row in world.rows()] == ["fail-open", "deny"]


@pytest.mark.feature("gate-import-failure-alerts")
def test_healthy_allow_deny_and_a_raising_gate_keep_their_results_and_send_no_alert(world, monkeypatch):
    monkeypatch.setitem(entry.GATES, "yes", Stub("yes", Decision()))
    monkeypatch.setitem(entry.GATES, "no", Stub("no", Decision.deny("no")))
    monkeypatch.setitem(entry.GATES, "boom", Stub("boom", KeyError("verdict")))
    assert (world.main("yes"), world.main("no"), world.main("boom")) == (0, 2, 0)
    assert [(row["gate"], row["kind"], row["reason"]) for row in world.rows()] == [
        ("no", "deny", "no"),
        ("boom", "fail-open", "KeyError: 'verdict'"),
    ]
    assert world.sent == []


@pytest.mark.feature("gate-import-failure-alerts")
def test_an_unknown_gate_name_still_exits_one_without_reading_the_payload(world, capsys):
    assert entry.main(["not-a-gate"], io.StringIO("not json"), dict(ENV), world.home) == 1
    assert "name one gate of" in capsys.readouterr().err
    assert world.rows() == [] and world.sent == []


@pytest.mark.feature("gate-import-failure-alerts")
def test_outside_a_swarm_the_call_goes_through_and_nothing_is_written(world, monkeypatch):
    monkeypatch.setitem(entry.GATES, "absent", missing("scripts.gates.absent_gate_module"))
    assert world.main("absent", environ={}) == 0
    assert world.rows() == [] and world.sent == []
    assert not world.home.exists()


@pytest.mark.feature("gate-import-failure-alerts")
def test_the_alert_waits_a_full_hour_per_gate_whatever_the_error(world, monkeypatch):
    monkeypatch.setitem(entry.GATES, "absent", missing("scripts.gates.absent_gate_module"))
    monkeypatch.setitem(entry.GATES, "other", missing("scripts.gates.other_absent_module"))
    assert world.main("absent") == 0
    world.now = START + HOUR - 1
    monkeypatch.setitem(entry.GATES, "absent", missing("scripts.gates.renamed_absent_module"))
    assert world.main("absent") == 0
    assert world.main("other") == 0
    world.now = START + HOUR
    assert world.main("absent") == 0
    assert len(world.rows()) == 4
    assert [op["item"] for _, op in world.sent] == ["gate-import-absent", "gate-import-other", "gate-import-absent"]


@pytest.mark.feature("gate-import-failure-alerts")
def test_an_unreachable_ledger_keeps_the_call_allowed_and_the_next_failure_retries(world, monkeypatch):
    monkeypatch.setitem(entry.GATES, "absent", missing("scripts.gates.absent_gate_module"))

    def down(slug: str, path: str, body: dict | None) -> dict:
        raise OSError("ledger down")

    monkeypatch.setattr(
        ledger, "client", lambda slug, timeout=10: ResourceClient("http://ledger.invalid", {}, timeout, down)
    )
    assert world.main("absent") == 0
    monkeypatch.setattr(
        ledger, "client", lambda slug, timeout=10: ResourceClient("http://ledger.invalid", {}, timeout, world.transport)
    )
    assert world.main("absent") == 0
    assert len(world.rows()) == 2
    assert world.alerts() == [(SLUG, "alert_raise", ME, "setup", "gate-import-absent")]


@pytest.mark.feature("gate-import-failure-alerts")
def test_an_unwritable_gate_log_still_lets_the_call_through_and_alerts(world, monkeypatch):
    monkeypatch.setitem(entry.GATES, "absent", missing("scripts.gates.absent_gate_module"))
    world.home.mkdir(parents=True)
    (world.home / SLUG).mkdir()
    (world.home / SLUG / "gates").mkdir()
    log.gate_log_path(SLUG, world.home).mkdir()
    assert world.main("absent") == 0
    assert world.alerts() == [(SLUG, "alert_raise", ME, "setup", "gate-import-absent")]


@pytest.mark.feature("gate-import-failure-alerts")
def test_the_native_gate_lookup_fails_open_on_an_import_failure(world, monkeypatch):
    monkeypatch.setitem(entry.HOST_GATES, "absent-host", missing("scripts.gates.absent_host_module"))
    monkeypatch.setattr("scripts.inbox.seen.receiving_session", lambda environ=None: "sid-import")
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("HOME", str(world.home.parent))
    assert entry.native("absent-host", "inbox_send") == 0
    rows = [json.loads(line) for line in log.gate_log_path(SLUG).read_text().splitlines()]
    assert [(row["gate"], row["kind"]) for row in rows] == [("absent-host", "fail-open")]
    assert world.alerts() == [(SLUG, "alert_raise", ME, "setup", "gate-import-absent-host")]


def run_processes(tmp_path: Path, now: float, gate: str, module: str, count: int = 1) -> list[int]:
    args = [str(tmp_path / "swarm"), str(tmp_path / "sent.jsonl"), str(now), gate, module, PAYLOAD, json.dumps(ENV)]
    started = [
        subprocess.Popen([sys.executable, "-c", PROCESS, *args], cwd=ROOT, stderr=subprocess.PIPE, text=True)
        for _ in range(count)
    ]
    return [proc.wait(timeout=60) for proc in started]


def sent_items(tmp_path: Path) -> list[str]:
    path = tmp_path / "sent.jsonl"
    return [json.loads(line)["item"] for line in path.read_text().splitlines()] if path.exists() else []


@pytest.mark.feature("gate-import-failure-alerts")
def test_separate_processes_share_one_alert_per_gate_per_hour(tmp_path):
    assert run_processes(tmp_path, START, "absent", "scripts.gates.absent_gate_module", count=6) == [0] * 6
    assert sent_items(tmp_path) == ["gate-import-absent"]
    assert run_processes(tmp_path, START + HOUR - 1, "absent", "scripts.gates.renamed_absent_module") == [0]
    assert run_processes(tmp_path, START + 1, "other", "scripts.gates.other_absent_module") == [0]
    assert sent_items(tmp_path) == ["gate-import-absent", "gate-import-other"]
    assert run_processes(tmp_path, START + HOUR, "absent", "scripts.gates.absent_gate_module") == [0]
    assert sent_items(tmp_path) == ["gate-import-absent", "gate-import-other", "gate-import-absent"]
    rows = log.gate_log_path(SLUG, tmp_path / "swarm").read_text().splitlines()
    assert len(rows) == 9
