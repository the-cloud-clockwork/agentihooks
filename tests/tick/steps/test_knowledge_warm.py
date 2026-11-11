"""The knowledge warm tick step starts a detached refresh that keeps a running swarm's knowledge index level."""

import fcntl
import json
import os
import subprocess
import sys
import time
from importlib import import_module
from pathlib import Path
from typing import Any, cast

import pytest

from scripts.ledger_knowledge import service, warm_job
from scripts.ledger_knowledge.index import HashEmbedder, KnowledgeIndex
from scripts.tick import registry
from scripts.tick.adapters import host
from scripts.tick.adapters.fakes import Call, FakeHost, FakeLedger, fake_tools
from scripts.tick.deadline import Deadline
from scripts.tick.engine import cadence_s, interval_s
from scripts.tick.steps import CallStep

pytestmark = pytest.mark.unit

KNOWLEDGE_WARM_JOB = True
SLUG = "sw"


def _step() -> CallStep:
    import_module("scripts.tick.steps.knowledge")
    found = {step.name: step for step in registry.steps()}["knowledge warm"]
    assert isinstance(found, CallStep)
    return found


def _document(*tasks: str) -> dict[str, Any]:
    return {
        "title": "Ledger",
        "overview": "what the ledger builds",
        "phases": [{"id": "p1", "title": "Phase one"}],
        "tasks": [{"id": task, "title": f"Task {task} about knowledge", "phase": "p1"} for task in tasks],
    }


def _environ(home: Path) -> dict[str, str]:
    return {"AGENTIHOOKS_HOME": str(home)}


def _index(home: Path) -> KnowledgeIndex:
    return KnowledgeIndex(service.index_path(SLUG, _environ(home)), HashEmbedder())


class _Repository:
    def __init__(self, document: dict[str, Any]) -> None:
        self.document = document
        self.reads: list[str] = []

    def get_document(self, slug: str) -> dict[str, Any]:
        self.reads.append(slug)
        return self.document


@pytest.fixture
def repository(monkeypatch: pytest.MonkeyPatch) -> _Repository:
    from scripts.swarm_ledger import storage

    stored = _Repository(_document("t1"))
    monkeypatch.setattr(storage, "default", lambda: stored)
    return stored


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Path, dict[str, str]]]:
    calls: list[tuple[str, Path, dict[str, str]]] = []
    monkeypatch.setattr(warm_job, "spawn", lambda lock, slug, log, environ: calls.append((slug, log, environ)))
    return calls


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    monkeypatch.delenv("AGENTIHOOKS_KNOWLEDGE_EMBEDDER", raising=False)
    return tmp_path


def _refresh(document: dict[str, Any], repository: _Repository, capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    repository.document = document
    lock = os.open(os.devnull, os.O_RDONLY)
    assert warm_job.main([str(lock), SLUG]) == 0
    with pytest.raises(OSError):
        os.fstat(lock)
    return json.loads(capsys.readouterr().out)


@pytest.mark.feature("tick-knowledge-warm")
def test_knowledge_warm() -> None:
    """The knowledge warm step registers in group wrapper metrics on the slow lane with its feature and ports."""
    found = _step()
    assert (found.group, found.lane, found.feature, found.effects, found.reads, found.needs) == (
        "wrapper metrics",
        "slow",
        "tick-knowledge-warm",
        frozenset({"host"}),
        frozenset(),
        frozenset(),
    )


@pytest.mark.feature("tick-knowledge-warm")
def test_the_step_runs_inside_the_window_an_ask_treats_as_fresh() -> None:
    assert interval_s(_step(), cadence_s({})) < service.REFRESH_SECONDS


@pytest.mark.feature("tick-knowledge-warm")
def test_the_step_starts_the_knowledge_job_under_its_deadline_and_reads_no_ledger() -> None:
    tools = fake_tools()
    deadline = Deadline.after(5)
    values = {"tools": tools.for_effects(frozenset({"host"})), "deadline": deadline}
    assert _step().call(import_module("scripts.swarm.tick"), values.__getitem__) is None
    assert isinstance(tools.host, FakeHost)
    assert isinstance(tools.ledger, FakeLedger)
    assert tools.host.calls == [Call("run", ({"job": "knowledge"},), deadline)]
    assert tools.ledger.calls == []


@pytest.mark.feature("tick-knowledge-warm")
def test_the_local_host_runs_the_knowledge_job_for_its_swarm(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(warm_job, "start", lambda slug, environ: calls.append((slug, environ)) or "k")
    environ = {"A": "1"}
    port = host.local_host(cast(Any, "store"), SLUG, environ)
    assert port.run({"job": "knowledge"}, deadline=Deadline.after(5)) == "k"
    assert calls == [(SLUG, environ)]


@pytest.mark.feature("tick-knowledge-warm")
def test_a_start_while_an_earlier_refresh_holds_the_lock_launches_nothing(
    _home: Path, spawned: list[tuple[str, Path, dict[str, str]]]
) -> None:
    environ = _environ(_home / "fresh")
    path = warm_job.lock_path(SLUG, environ)
    assert path == _home / "fresh" / "knowledge" / "sw.warm.lock"
    assert warm_job.start(SLUG, environ) is True
    assert spawned == [(SLUG, _home / "fresh" / "knowledge" / "sw.warm.log", environ)]
    with path.open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert warm_job.start(SLUG, environ) is False
    assert len(spawned) == 1
    assert warm_job.start(SLUG, environ) is True
    assert len(spawned) == 2


@pytest.mark.feature("tick-knowledge-warm")
def test_a_semantic_embedder_setting_launches_nothing(
    _home: Path, spawned: list[tuple[str, Path, dict[str, str]]]
) -> None:
    environ = {**_environ(_home), "AGENTIHOOKS_KNOWLEDGE_EMBEDDER": "semantic"}
    assert warm_job.start(SLUG, environ) is False
    assert spawned == []
    assert not (_home / "knowledge").exists()


@pytest.mark.feature("tick-knowledge-warm")
def test_the_job_is_launched_detached_holding_the_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    launched: list[tuple[list[str], dict[str, Any]]] = []
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **options: launched.append((argv, options)))
    log = tmp_path / "warm.log"
    warm_job.spawn(7, SLUG, log, {"AGENTIHOOKS_HOME": "/home/x"})
    assert log.exists()
    [(argv, options)] = launched
    assert argv == [sys.executable, "-m", "scripts.ledger_knowledge.warm_job", "7", SLUG]
    assert options["stderr"].name == str(log)
    assert {name: value for name, value in options.items() if name != "stderr"} == {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "start_new_session": True,
        "pass_fds": (7,),
        "env": {"AGENTIHOOKS_HOME": "/home/x"},
    }


@pytest.mark.feature("tick-knowledge-warm")
def test_a_task_added_to_the_ledger_is_in_the_index_after_one_job(
    _home: Path, repository: _Repository, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _refresh(_document("t1"), repository, capsys)["added"] == 3
    assert _refresh(_document("t1", "t2"), repository, capsys)["added"] == 1
    assert repository.reads == [SLUG, SLUG]
    hits = _index(_home).query("Task t2 about knowledge", 1)
    assert [(hit.chunk.ref, [parent.ref for parent in hit.parents]) for hit in hits] == [
        ("tasks/t2", ["phases/p1", "ledger"])
    ]


@pytest.mark.feature("tick-knowledge-warm")
def test_an_ask_right_after_the_job_ran_answers_without_reading_the_ledger(
    _home: Path, repository: _Repository, capsys: pytest.CaptureFixture[str]
) -> None:
    _refresh(_document("t1", "t2"), repository, capsys)

    def unread() -> dict[str, Any]:
        raise AssertionError("ask refreshed a warm index")

    answer = service.ask(_index(_home), unread, "Task t2 about knowledge", int(time.time() * 1000))
    assert answer["hits"][0]["ref"] == "tasks/t2"
