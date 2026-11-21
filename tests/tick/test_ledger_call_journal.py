"""Each ledger request a tick pass makes through the in process ledger client's transport journals its wall time."""

import io
import json
import urllib.error
from collections.abc import Iterator
from email.message import Message
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.swarm import timing
from scripts.tick.adapters import database_ledger

pytestmark = pytest.mark.unit


class Answers:
    """A ledger route that answers every request, or raises ``error``."""

    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def __call__(self, slug: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append((slug, path, payload))
        if self.error is not None:
            raise self.error
        return {"data": {"answered": path}}


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> None:
    ticks: Iterator[float] = iter([5.0, 7.5])
    monkeypatch.setattr(database_ledger, "time", SimpleNamespace(monotonic=lambda: next(ticks)))


@pytest.fixture
def in_pass() -> Iterator[None]:
    token = timing.PASS.set({"pass_id": "p1", "failed_steps": []})
    try:
        with timing.tick("sw"):
            yield
    finally:
        timing.PASS.reset(token)


def journaled(err: str) -> list[dict[str, object]]:
    lines = [json.loads(line) for line in err.splitlines() if line.startswith("{")]
    return [line for line in lines if line.get("event") == "swarm_tick_ledger_call"]


def answered(monkeypatch: pytest.MonkeyPatch, error: BaseException | None = None) -> Answers:
    answers = Answers(error)
    monkeypatch.setattr(database_ledger, "answered", answers)
    return answers


def line(method: str, outcome: str) -> dict[str, object]:
    return {
        "event": "swarm_tick_ledger_call",
        "slug": "sw",
        "pass_id": "p1",
        "ledger": "led",
        "method": method,
        "path": "tasks",
        "outcome": outcome,
        "wall_s": 2.5,
    }


@pytest.mark.feature("swarm-tick-ledger-call-journal")
@pytest.mark.usefixtures("clock", "in_pass")
def test_a_read_inside_a_pass_journals_its_ledger_route_outcome_and_wall_seconds(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    answers = answered(monkeypatch)
    reply = database_ledger.client("in-process", 3.0).request("led", "tasks?limit=100")
    assert reply == {"data": {"answered": "tasks?limit=100"}}
    assert answers.calls == [("led", "tasks?limit=100", None)]
    assert journaled(capsys.readouterr().err) == [line("GET", "ok")]


@pytest.mark.feature("swarm-tick-ledger-call-journal")
@pytest.mark.usefixtures("clock", "in_pass")
def test_a_refused_write_inside_a_pass_journals_its_status_and_still_raises(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    refusal = urllib.error.HTTPError("/api", 409, "ledger refused", Message(), io.BytesIO(b"{}"))
    answered(monkeypatch, refusal)
    with pytest.raises(urllib.error.HTTPError) as raised:
        database_ledger.client("in-process", 3.0).request("led", "tasks", {"ops": []})
    assert raised.value is refusal
    assert journaled(capsys.readouterr().err) == [line("POST", "409")]


@pytest.mark.feature("swarm-tick-ledger-call-journal")
@pytest.mark.usefixtures("clock", "in_pass")
def test_a_failure_other_than_a_refusal_inside_a_pass_journals_an_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    answered(monkeypatch, OSError("storage down"))
    with pytest.raises(OSError, match="storage down"):
        database_ledger.client("in-process", 3.0).request("led", "tasks")
    assert journaled(capsys.readouterr().err) == [line("GET", "error")]


@pytest.mark.feature("swarm-tick-ledger-call-journal")
def test_a_request_outside_a_pass_journals_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    answers = answered(monkeypatch)
    with timing.tick("sw"):
        reply = database_ledger.client("in-process", 3.0).request("led", "tasks", {"ops": []})
    assert reply == {"data": {"answered": "tasks"}}
    assert answers.calls == [("led", "tasks", {"ops": []})]
    assert journaled(capsys.readouterr().err) == []
