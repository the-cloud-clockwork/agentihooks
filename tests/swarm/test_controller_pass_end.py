"""The controller writes one structured line when each pass ends, naming the pass and its outcome."""

import json
import urllib.error
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast

import pytest
import redis

from scripts.gates import intent
from scripts.swarm import commands, controller, ledger_client, redis_grace
from scripts.swarm.controller_probes import HOST_SWARMS_ENV
from scripts.swarm.ledger_client import LedgerClient, LedgerUnreachable
from scripts.swarm.store import RedisStore

pytestmark = pytest.mark.unit

STORE = cast(RedisStore, None)
PROOF = ModuleType("proof_swarm")


class StopLoop(Exception):
    """Raised by the fake pass to leave the run loop once the test has seen enough passes."""


class Clock:
    """A fixed clock for the Redis grace window."""

    def __call__(self) -> float:
        return 0.0


def ended(text: str) -> list[dict[str, object]]:
    """Return every pass end line written to standard output, parsed."""
    lines = [json.loads(line) for line in text.splitlines() if line.startswith("{")]
    return [line for line in lines if line.get("event") == "controller_pass_end"]


def run_passes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, outcomes: list[BaseException | None]) -> list[int]:
    """Make each controller pass raise the next listed error, or return when it is None."""
    calls: list[int] = []
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    monkeypatch.setattr(intent, "keep_judging", lambda: None)
    monkeypatch.setattr(controller, "scheduled_wait", lambda seconds: None)

    def run(store: RedisStore, service: object, proof_swarm: ModuleType) -> None:
        calls.append(1)
        error = outcomes[len(calls) - 1]
        if error is not None:
            raise error

    monkeypatch.setattr(controller, "_pass", run)
    return calls


@pytest.mark.feature("controller-pass-end-line")
def test_each_pass_writes_one_end_line_naming_its_number_and_outcome(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = run_passes(
        monkeypatch,
        tmp_path,
        [None, LedgerUnreachable("ledger timed out"), redis.ConnectionError("refused"), None, StopLoop()],
    )

    with pytest.raises(StopLoop):
        controller._passes(STORE, None, PROOF, False, redis_grace.Outage(30, Clock()))

    assert len(calls) == 5
    assert ended(capsys.readouterr().out) == [
        {"event": "controller_pass_end", "pass": 1, "outcome": "ok"},
        {"event": "controller_pass_end", "pass": 2, "outcome": "ledger_unreachable"},
        {"event": "controller_pass_end", "pass": 3, "outcome": "redis_grace"},
        {"event": "controller_pass_end", "pass": 4, "outcome": "ok"},
    ]


@pytest.mark.feature("controller-pass-end-line")
def test_a_single_pass_writes_its_end_line_before_returning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_passes(monkeypatch, tmp_path, [None])

    assert controller._passes(STORE, None, PROOF, True, redis_grace.Outage(30, Clock())) == 0
    assert ended(capsys.readouterr().out) == [{"event": "controller_pass_end", "pass": 1, "outcome": "ok"}]


@pytest.mark.feature("controller-pass-end-line")
@pytest.mark.parametrize(
    ("error", "outcome"),
    [(LedgerUnreachable("ledger timed out"), "ledger_unreachable"), (redis.ConnectionError("refused"), "redis_grace")],
)
def test_a_single_failed_pass_names_its_failure_in_the_end_line(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    error: BaseException,
    outcome: str,
) -> None:
    run_passes(monkeypatch, tmp_path, [error])

    assert controller._passes(STORE, None, PROOF, True, redis_grace.Outage(30, Clock())) == 1
    assert ended(capsys.readouterr().out) == [{"event": "controller_pass_end", "pass": 1, "outcome": outcome}]


class Stdout:
    """A standard output double that records each write and flush in order."""

    def __init__(self) -> None:
        self.events: list[str] = []

    def write(self, text: str) -> int:
        self.events.append(text)
        return len(text)

    def flush(self) -> None:
        self.events.append("<flush>")


@pytest.mark.feature("controller-pass-end-line")
def test_the_end_line_is_flushed_so_a_piped_log_reader_sees_it_at_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stdout = Stdout()
    run_passes(monkeypatch, tmp_path, [None])
    monkeypatch.setattr(controller.sys, "stdout", stdout)

    assert controller._passes(STORE, None, PROOF, True, redis_grace.Outage(30, Clock())) == 0
    assert stdout.events == ['{"event": "controller_pass_end", "pass": 1, "outcome": "ok"}\n', "<flush>"]


class RefusedLedger:
    """A ledger that refuses the connection on the first closed check and stops the loop on the second."""

    def __init__(self) -> None:
        self.reads: list[str] = []

    def binned(self, slug: str) -> bool:
        assert slug == "proof"
        return False

    def closed(self, slug: str) -> bool:
        self.reads.append(slug)
        if len(self.reads) > 1:
            raise StopLoop()
        raise ConnectionRefusedError(f"connection refused reading {slug}")


@pytest.mark.feature("controller-survives-ledger-connection-failure")
def test_a_refused_closed_check_ends_the_pass_and_the_controller_runs_the_next_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = RefusedLedger()
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    monkeypatch.delenv(HOST_SWARMS_ENV, raising=False)
    monkeypatch.setattr(intent, "keep_judging", lambda: None)
    monkeypatch.setattr(controller, "scheduled_wait", lambda seconds: None)
    monkeypatch.setattr(commands, "hive_id", lambda: "hive")
    monkeypatch.setattr(controller, "LedgerClient", lambda: ledger)
    store = cast(RedisStore, SimpleNamespace(slugs=lambda: ["proof"]))

    with pytest.raises(StopLoop):
        controller._passes(store, None, PROOF, False, redis_grace.Outage(30, Clock()))

    output = capsys.readouterr()
    assert ledger.reads == ["proof", "proof"]
    assert ended(output.out) == [{"event": "controller_pass_end", "pass": 1, "outcome": "ledger_unreachable"}]
    assert (
        "controller: pass ended, next pass after one tick: ledger proof: connection refused reading proof" in output.err
    )


@pytest.mark.feature("controller-survives-ledger-connection-failure")
def test_an_unreachable_remote_lifecycle_read_raises_ledger_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    def request(slug: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        assert (slug, path, payload) == ("proof", "lifecycle/actions", {"action": "binned"})
        raise urllib.error.URLError("connection refused")

    def client(slug: str, service: bool) -> SimpleNamespace:
        assert (slug, service) == ("proof", False)
        return SimpleNamespace(request=request)

    monkeypatch.setattr(ledger_client, "_ledger", lambda: SimpleNamespace(client=client))

    with pytest.raises(LedgerUnreachable, match=r"^ledger proof: <urlopen error connection refused>$"):
        LedgerClient()._lifecycle("proof", {"action": "binned"})
