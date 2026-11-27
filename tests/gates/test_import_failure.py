"""A gate whose implementation cannot be imported lets the call through, writes a fail-open row naming the gate and the
import error, and raises one setup alert per gate per hour naming the gate and the interpreter."""

import fcntl
import io
import json
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from importlib import import_module
from pathlib import Path
from typing import Any

import pytest

from scripts.gates import entry, import_failure, log
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
        self.home, self.now, self.sent, self.clients = home, START, [], []

    def clock(self) -> float:
        return self.now

    def client(self, slug: str, timeout: float = 10) -> ResourceClient:
        self.clients.append((slug, timeout))
        return ResourceClient("http://ledger.invalid", {}, timeout, self.transport)

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
    monkeypatch.setattr(ledger, "client", held.client)
    return held


def missing(module: str) -> tuple:
    return (lambda: import_module(module),)


def summary(rows: list[dict]) -> list[tuple[str, str, str, str, str]]:
    return [(row["gate"], row["kind"], row["agent"], row["task"], row["tool"]) for row in rows]


@pytest.mark.feature("gate-import-failure-alerts")
def test_a_gate_whose_module_is_missing_lets_the_call_through_with_a_row_and_an_alert(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
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
def test_a_gate_missing_a_transitive_dependency_is_reported_under_its_registered_name(
    world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "gate_with_a_lost_dependency.py").write_text("import a_dependency_that_is_not_installed\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setitem(entry.GATES, "reruns", missing("gate_with_a_lost_dependency"))
    assert world.main("reruns") == 0
    rows = world.rows()
    assert summary(rows) == [("reruns", "fail-open", ME, TASK, "Bash")]
    assert "No module named 'a_dependency_that_is_not_installed'" in rows[0]["reason"]
    assert world.alerts() == [(SLUG, "alert_raise", ME, "setup", "gate-import-reruns")]


@pytest.mark.feature("gate-import-failure-alerts")
def test_a_healthy_gate_named_on_the_same_call_still_denies(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(entry.GATES, "absent", missing("scripts.gates.absent_gate_module"))
    monkeypatch.setitem(entry.GATES, "stub", Stub("stub", Decision.deny("stub says no")))
    assert world.main("absent", "stub") == 2
    assert [row["kind"] for row in world.rows()] == ["fail-open", "deny"]


@pytest.mark.feature("gate-import-failure-alerts")
def test_healthy_allow_deny_and_a_raising_gate_keep_their_results_and_send_no_alert(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
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
def test_an_unknown_gate_name_still_exits_one_without_reading_the_payload(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    assert entry.main(["not-a-gate"], io.StringIO("not json"), dict(ENV), world.home) == 1
    assert "name one gate of" in capsys.readouterr().err
    assert world.rows() == [] and world.sent == []


@pytest.mark.feature("gate-import-failure-alerts")
def test_outside_a_swarm_the_call_goes_through_and_nothing_is_written(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(entry.GATES, "absent", missing("scripts.gates.absent_gate_module"))
    assert world.main("absent", environ={}) == 0
    assert world.rows() == [] and world.sent == []
    assert not world.home.exists()


@pytest.mark.feature("gate-import-failure-alerts")
def test_the_alert_waits_a_full_hour_per_gate_whatever_the_error(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
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
def test_an_unreachable_ledger_keeps_the_call_allowed_and_the_next_failure_retries(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
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
def test_an_unwritable_gate_log_still_lets_the_call_through_and_alerts(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(entry.GATES, "absent", missing("scripts.gates.absent_gate_module"))
    world.home.mkdir(parents=True)
    (world.home / SLUG).mkdir()
    (world.home / SLUG / "gates").mkdir()
    log.gate_log_path(SLUG, world.home).mkdir()
    assert world.main("absent") == 0
    assert world.alerts() == [(SLUG, "alert_raise", ME, "setup", "gate-import-absent")]


@pytest.mark.feature("gate-import-failure-alerts")
def test_the_native_gate_lookup_fails_open_on_an_import_failure(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
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
def test_separate_processes_share_one_alert_per_gate_per_hour(tmp_path: Path) -> None:
    assert run_processes(tmp_path, START, "absent", "scripts.gates.absent_gate_module", count=6) == [0] * 6
    assert sent_items(tmp_path) == ["gate-import-absent"]
    assert run_processes(tmp_path, START + HOUR - 1, "absent", "scripts.gates.renamed_absent_module") == [0]
    assert run_processes(tmp_path, START + 1, "other", "scripts.gates.other_absent_module") == [0]
    assert sent_items(tmp_path) == ["gate-import-absent", "gate-import-other"]
    assert run_processes(tmp_path, START + HOUR, "absent", "scripts.gates.absent_gate_module") == [0]
    assert sent_items(tmp_path) == ["gate-import-absent", "gate-import-other", "gate-import-absent"]
    rows = log.gate_log_path(SLUG, tmp_path / "swarm").read_text().splitlines()
    assert len(rows) == 9


def broken_by(error: BaseException) -> tuple:
    def factory() -> object:
        raise error

    return (factory,)


def stamps(home: Path) -> dict:
    return json.loads((log.gates_dir(SLUG, home) / "import-alerts.json").read_text())


@pytest.mark.feature("gate-import-failure-alerts")
def test_the_alert_carries_its_whole_text_and_an_operation_id_through_a_bounded_client(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(entry.GATES, "absent", missing("scripts.gates.absent_gate_module"))
    assert world.main("absent") == 0
    op = world.sent[0][1]
    assert op["text"] == (
        f"The absent gate cannot import its implementation under {sys.executable} and lets every call through until "
        "that interpreter is repaired: ModuleNotFoundError: No module named 'scripts.gates.absent_gate_module'"
    )
    assert re.fullmatch(r"alert_raise-[0-9a-f]{10}", op["id"])
    assert world.clients == [(SLUG, 2.0)]
    assert stamps(world.home) == {"absent": int(START * 1000)}
    assert world.rows()[0]["at"] == int(START * 1000)


@pytest.mark.feature("gate-import-failure-alerts")
def test_a_swarm_member_without_a_name_alerts_as_the_swarm(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(entry.GATES, "absent", missing("scripts.gates.absent_gate_module"))
    assert world.main("absent", environ={"AGENTIHOOKS_SWARM": SLUG}) == 0
    assert [op["by"] for _, op in world.sent] == ["swarm"]


@pytest.mark.feature("gate-import-failure-alerts")
def test_a_long_import_error_is_cut_to_the_alert_limit(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.swarm_ledger import ledger_alerts

    monkeypatch.setitem(entry.GATES, "absent", broken_by(ImportError("x" * 3000)))
    assert world.main("absent") == 0
    assert len(world.sent[0][1]["text"]) == ledger_alerts.MAX_OUTCOME
    assert world.rows()[0]["reason"].endswith("ImportError: " + "x" * 3000)


@pytest.mark.feature("gate-import-failure-alerts")
@pytest.mark.parametrize(
    "error",
    [
        AttributeError("get"),
        ImportError("gone"),
        KeyError("revision"),
        OSError("down"),
        RuntimeError("refused"),
        SystemExit("no url"),
    ],
)
def test_any_send_failure_frees_the_hour_and_says_so(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], error: BaseException
) -> None:
    monkeypatch.setitem(entry.GATES, "absent", missing("scripts.gates.absent_gate_module"))

    def failing(slug: str, path: str, body: dict | None) -> dict:
        raise error

    monkeypatch.setattr(ledger, "client", lambda slug, timeout=10: ResourceClient("http://x", {}, timeout, failing))
    assert world.main("absent") == 0
    assert f"agentihooks gate absent: alert not sent: {type(error).__name__}: " in capsys.readouterr().err
    assert stamps(world.home) == {}


@pytest.mark.feature("gate-import-failure-alerts")
def test_a_value_error_from_the_client_frees_the_hour(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(entry.GATES, "absent", missing("scripts.gates.absent_gate_module"))

    def refused(slug: str, timeout: float = 10) -> ResourceClient:
        raise ValueError("one batch pins alerts to two revisions")

    monkeypatch.setattr(ledger, "client", refused)
    assert world.main("absent") == 0
    assert stamps(world.home) == {}


@pytest.mark.feature("gate-import-failure-alerts")
def test_unwritable_gate_state_lets_the_call_through_and_names_both_misses(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(entry.GATES, "absent", missing("scripts.gates.absent_gate_module"))
    log.gates_dir(SLUG, world.home).parent.mkdir(parents=True)
    log.gates_dir(SLUG, world.home).write_text("")
    assert world.main("absent") == 0
    err = capsys.readouterr().err
    assert "agentihooks gate absent: gate log row not written: " in err
    assert "agentihooks gate absent: alert hour not claimed: " in err
    assert world.sent == []


@pytest.mark.feature("gate-import-failure-alerts")
def test_a_claim_that_cannot_be_freed_is_named(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(entry.GATES, "absent", missing("scripts.gates.absent_gate_module"))

    def down(slug: str, path: str, body: dict | None) -> dict:
        raise OSError("ledger down")

    def stuck(slug: str, gate: str, stamp_ms: int, home: Path | str | None = None) -> None:
        raise PermissionError("read only")

    monkeypatch.setattr(ledger, "client", lambda slug, timeout=10: ResourceClient("http://x", {}, timeout, down))
    monkeypatch.setattr(import_failure, "release", stuck)
    assert world.main("absent") == 0
    assert "agentihooks gate absent: alert hour not freed: PermissionError: read only" in capsys.readouterr().err


@pytest.mark.feature("gate-import-failure-alerts")
def test_a_claim_drops_stamps_an_hour_old_and_keeps_younger_ones(tmp_path: Path) -> None:
    now = int(START * 1000)
    assert import_failure.claim(SLUG, "old", now, tmp_path)
    assert import_failure.claim(SLUG, "young", now + 1, tmp_path)
    assert import_failure.claim(SLUG, "new", now + 3_600_000, tmp_path)
    assert stamps(tmp_path) == {"young": now + 1, "new": now + 3_600_000}
    assert not import_failure.claim(SLUG, "young", now + 3_600_000, tmp_path)


@pytest.mark.feature("gate-import-failure-alerts")
def test_release_drops_only_the_stamp_it_claimed(tmp_path: Path) -> None:
    now = int(START * 1000)
    assert import_failure.claim(SLUG, "absent", now, tmp_path)
    import_failure.release(SLUG, "absent", now + 1, tmp_path)
    assert stamps(tmp_path) == {"absent": now}
    import_failure.release(SLUG, "absent", now, tmp_path)
    assert stamps(tmp_path) == {}
    import_failure.release(SLUG, "other", now, tmp_path)
    assert stamps(tmp_path) == {}


@pytest.mark.feature("gate-import-failure-alerts")
def test_a_corrupt_stamp_file_is_read_as_empty(tmp_path: Path) -> None:
    log.gates_dir(SLUG, tmp_path).mkdir(parents=True)
    (log.gates_dir(SLUG, tmp_path) / "import-alerts.json").write_text("{not json")
    assert import_failure.claim(SLUG, "absent", int(START * 1000), tmp_path)


@pytest.mark.feature("gate-import-failure-alerts")
def test_simultaneous_claims_in_one_process_give_the_hour_once(tmp_path: Path) -> None:
    def take(_: int) -> bool:
        return import_failure.claim(SLUG, "absent", int(START * 1000), tmp_path)

    with ThreadPoolExecutor(max_workers=8) as pool:
        taken = list(pool.map(take, range(16)))
    assert taken.count(True) == 1


@pytest.mark.feature("gate-import-failure-alerts")
def test_no_gate_name_exits_one(world: World) -> None:
    assert entry.main([], io.StringIO(PAYLOAD), dict(ENV), world.home) == 1


@pytest.mark.feature("gate-import-failure-alerts")
def test_a_held_lock_is_tried_a_bounded_number_of_times(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pauses: list[float] = []
    monkeypatch.setattr(import_failure.time, "sleep", pauses.append)
    lock = log.gates_dir(SLUG, tmp_path) / "import-alerts.lock"
    lock.parent.mkdir(parents=True)
    with lock.open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        with pytest.raises(TimeoutError, match="stayed locked"):
            import_failure.claim(SLUG, "absent", int(START * 1000), tmp_path)
    assert pauses == [0.05] * 20


@pytest.mark.feature("gate-import-failure-alerts")
def test_a_lock_freed_between_tries_is_taken(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lock = log.gates_dir(SLUG, tmp_path) / "import-alerts.lock"
    lock.parent.mkdir(parents=True)
    held = lock.open("w")
    fcntl.flock(held, fcntl.LOCK_EX)
    monkeypatch.setattr(import_failure.time, "sleep", lambda pause: held.close())
    assert import_failure.claim(SLUG, "absent", int(START * 1000), tmp_path)
    assert stamps(tmp_path) == {"absent": int(START * 1000)}


@pytest.mark.feature("gate-import-failure-alerts")
def test_a_held_lock_skips_the_alert_and_keeps_the_call(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(entry.GATES, "absent", missing("scripts.gates.absent_gate_module"))
    monkeypatch.setattr(import_failure.time, "sleep", lambda pause: None)
    lock = log.gates_dir(SLUG, world.home) / "import-alerts.lock"
    lock.parent.mkdir(parents=True)
    with lock.open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert world.main("absent") == 0
    assert "agentihooks gate absent: alert hour not claimed: TimeoutError: " in capsys.readouterr().err
    assert len(world.rows()) == 1 and world.sent == []


@pytest.mark.feature("gate-import-failure-alerts")
@pytest.mark.parametrize("held", ["[]", '"stamp"', '{"absent": "soon", "other": 5}'])
def test_a_stamp_file_of_another_shape_keeps_only_whole_number_stamps(tmp_path: Path, held: str) -> None:
    log.gates_dir(SLUG, tmp_path).mkdir(parents=True)
    (log.gates_dir(SLUG, tmp_path) / "import-alerts.json").write_text(held)
    assert import_failure.claim(SLUG, "absent", 5, tmp_path)
    assert stamps(tmp_path)["absent"] == 5
    assert stamps(tmp_path).get("other") in (None, 5)


@pytest.mark.feature("gate-import-failure-alerts")
def test_a_malformed_ledger_reply_frees_the_hour(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(entry.GATES, "absent", missing("scripts.gates.absent_gate_module"))

    def listing(slug: str, path: str, body: dict | None) -> Any:
        return []

    monkeypatch.setattr(ledger, "client", lambda slug, timeout=10: ResourceClient("http://x", {}, timeout, listing))
    assert world.main("absent") == 0
    assert stamps(world.home) == {}
