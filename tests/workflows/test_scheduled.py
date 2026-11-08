import inspect
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from hooks import config
from scripts.scheduler import slots
from scripts.workflows import scheduled


class Spawn:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple[list[str], dict]] = []
        self.error = error

    def __call__(self, argv: list[str], **options: object) -> None:
        self.calls.append((argv, options))
        if self.error is not None:
            raise self.error


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    root = tmp_path / "home"
    monkeypatch.setattr(config, "AGENTIHOOKS_HOME", str(root))
    return root


def test_start_spawns_one_detached_run_named_by_the_delivery_id(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "case.md").write_text("case")
    spawn = Spawn()

    assert scheduled.start("tiny", "case.md", "entry-1:slot-1", popen=spawn) is True

    folder = home / "workflows" / "deliveries" / "entry-1:slot-1"
    [(argv, options)] = spawn.calls
    assert argv == [
        sys.executable,
        "-m",
        "scripts.install",
        "workflow",
        "run",
        "tiny",
        "--case",
        str(tmp_path / "case.md"),
        "--run-id",
        "entry-1:slot-1",
    ]
    assert options["stdin"] == subprocess.DEVNULL
    assert options["start_new_session"] is True
    assert options["stdout"] is options["stderr"]
    assert options["stdout"].name == str(folder / "run.log")
    assert options["stdout"].mode == "ab"
    assert options["stdout"].closed
    assert (folder / "run.log").exists()


def test_start_spawns_through_popen_by_default() -> None:
    assert inspect.signature(scheduled.start).parameters["popen"].default is subprocess.Popen


def test_a_redelivery_of_the_same_id_spawns_nothing(home: Path) -> None:
    spawn = Spawn()

    assert scheduled.start("tiny", "/case.md", "entry-1:slot-1", popen=spawn) is True
    assert scheduled.start("tiny", "/case.md", "entry-1:slot-1", popen=spawn) is False
    assert scheduled.start("tiny", "/case.md", "entry-2:slot-1", popen=spawn) is True

    assert [argv[-1] for argv, _ in spawn.calls] == ["entry-1:slot-1", "entry-2:slot-1"]


def test_a_failed_spawn_leaves_the_delivery_free_for_a_retry(home: Path) -> None:
    with pytest.raises(OSError) as raised:
        scheduled.start("tiny", "/case.md", "entry-1:slot-1", popen=Spawn(OSError("no python")))

    assert str(raised.value) == "no python"

    assert not (home / "workflows" / "deliveries" / "entry-1:slot-1").exists()
    retry = Spawn()
    assert scheduled.start("tiny", "/case.md", "entry-1:slot-1", popen=retry) is True
    assert len(retry.calls) == 1


@pytest.mark.parametrize("delivery_id", ["", "..", "a/b"], ids=["empty", "parent", "nested"])
def test_a_delivery_id_that_is_not_one_name_is_refused(home: Path, delivery_id: str) -> None:
    spawn = Spawn()

    with pytest.raises(ValueError) as raised:
        scheduled.start("tiny", "/case.md", delivery_id, popen=spawn)

    assert str(raised.value) == "run identifier must be a single name"

    assert spawn.calls == []
    assert not (home / "workflows" / "deliveries").exists()


def test_the_workflow_kind_round_trips_its_record() -> None:
    slot = slots.WorkflowSlot("tiny", "/case.md")

    assert slots.KINDS["workflow"] is slots.WorkflowSlot
    assert slot.record() == {"kind": "workflow", "name": "tiny", "case": "/case.md"}
    assert slots.restore(slot.record()) == slot


def test_the_workflow_slot_delivers_through_start(monkeypatch: pytest.MonkeyPatch) -> None:
    started = []
    monkeypatch.setattr(scheduled, "start", lambda *args: started.append(args) or True)

    assert slots.WorkflowSlot("tiny", "/case.md").deliver({"owner": "me"}, "entry-1:slot-1", cast(Any, None)) is None

    assert started == [("tiny", "/case.md", "entry-1:slot-1")]
