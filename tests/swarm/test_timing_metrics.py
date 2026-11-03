from typing import Any

import pytest

from scripts.swarm import timing

pytestmark = pytest.mark.unit


@pytest.fixture
def exported(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, dict[str, Any]]]:
    calls: list[tuple[str, str, dict[str, Any]]] = []
    monkeypatch.setattr(timing.tick_metrics, "step_finished", lambda slug, record: calls.append(("step", slug, record)))
    monkeypatch.setattr(timing.tick_metrics, "pass_finished", lambda slug, record: calls.append(("pass", slug, record)))
    return calls


def test_every_finished_step_exports_its_finished_record(
    exported: list[tuple[str, str, dict[str, Any]]], capsys: pytest.CaptureFixture[str]
) -> None:
    with timing.tick("sw"):
        timing.within(5.0, lambda: None, lane="slow", name="rank")
        with pytest.raises(ValueError), timing.step("launch"):
            raise ValueError("boom")
    assert [(kind, slug, row["step"], row["phase"], row["outcome"]) for kind, slug, row in exported] == [
        ("step", "sw", "rank", "finished", "success"),
        ("step", "sw", "launch", "finished", "error"),
    ]
    assert exported[0][2]["lane"] == "slow"


def test_a_step_outside_a_tick_exports_nothing(exported: list[tuple[str, str, dict[str, Any]]]) -> None:
    with timing.step("launch"):
        pass
    assert exported == []


def test_a_finished_pass_exports_its_record_before_saving_it(
    exported: list[tuple[str, str, dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    saved: list[tuple[object, str, dict[str, Any]]] = []
    monkeypatch.setattr(timing, "finish_pass", lambda store, slug, record: saved.append((store, slug, dict(record))))
    store = object()
    with pytest.raises(RuntimeError), timing.pass_record(store, "sw"):
        raise RuntimeError("boom")
    assert len(saved) == 1 and saved[0][0] is store
    assert [(kind, slug, row["outcome"], row["pass_id"]) for kind, slug, row in exported] == [
        ("pass", "sw", "error", saved[0][2]["pass_id"])
    ]
    assert exported[0][2]["end_ms"] == saved[0][2]["end_ms"]


def test_a_pass_without_a_store_exports_nothing(exported: list[tuple[str, str, dict[str, Any]]]) -> None:
    with timing.pass_record(None, "sw"):
        pass
    assert exported == []
