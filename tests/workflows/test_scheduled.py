import inspect
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from hooks import config
from scripts.inbox import store as inbox_store
from scripts.inbox.store import DuplicateItem
from scripts.scheduler import slots
from scripts.workflows import scheduled, store


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

    assert scheduled.start("tiny", "case.md", "entry-1:slot-1", "me", popen=spawn) is True

    folder = home / "workflows" / "deliveries" / "entry-1:slot-1"
    [(argv, options)] = spawn.calls
    assert argv == [
        sys.executable,
        "-m",
        "scripts.workflows.scheduled",
        "me",
        "tiny",
        str(tmp_path / "case.md"),
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

    assert scheduled.start("tiny", "/case.md", "entry-1:slot-1", "me", popen=spawn) is True
    assert scheduled.start("tiny", "/case.md", "entry-1:slot-1", "me", popen=spawn) is False
    assert scheduled.start("tiny", "/case.md", "entry-2:slot-1", "me", popen=spawn) is True

    assert [argv[-1] for argv, _ in spawn.calls] == ["entry-1:slot-1", "entry-2:slot-1"]


def test_a_failed_spawn_leaves_the_delivery_free_for_a_retry(home: Path) -> None:
    with pytest.raises(OSError) as raised:
        scheduled.start("tiny", "/case.md", "entry-1:slot-1", "me", popen=Spawn(OSError("no python")))

    assert str(raised.value) == "no python"

    assert not (home / "workflows" / "deliveries" / "entry-1:slot-1").exists()
    retry = Spawn()
    assert scheduled.start("tiny", "/case.md", "entry-1:slot-1", "me", popen=retry) is True
    assert len(retry.calls) == 1


@pytest.mark.parametrize("delivery_id", ["", "..", "a/b"], ids=["empty", "parent", "nested"])
def test_a_delivery_id_that_is_not_one_name_is_refused(home: Path, delivery_id: str) -> None:
    spawn = Spawn()

    with pytest.raises(ValueError) as raised:
        scheduled.start("tiny", "/case.md", delivery_id, "me", popen=spawn)

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

    assert started == [("tiny", "/case.md", "entry-1:slot-1", "me")]


class Inbox:
    def __init__(self, duplicate: bool = False) -> None:
        self.sent: list[tuple[tuple[str, ...], dict[str, str]]] = []
        self.duplicate = duplicate

    def send(self, *args: str, **options: str) -> None:
        self.sent.append((args, options))
        if self.duplicate:
            raise DuplicateItem("message was already sent")


def finished_run(home: Path, run_id: str, status: str) -> Path:
    folder = home / "workflows" / "runs" / run_id
    folder.mkdir(parents=True)
    store.save(folder, {"status": status})
    return folder


def test_a_finished_run_sends_its_status_and_folder_to_the_owner(home: Path) -> None:
    folder = finished_run(home, "entry-1:slot-1", "passed")
    inbox = Inbox()

    scheduled.notify("me", "entry-1:slot-1", inbox=cast(Any, inbox))

    assert inbox.sent == [
        (
            ("scheduler", "me", f"workflow run entry-1:slot-1 passed, run folder {folder}"),
            {"item_id": "entry-1:slot-1:done"},
        )
    ]


def test_a_run_that_saved_no_state_reports_failed(home: Path) -> None:
    inbox = Inbox()

    scheduled.notify("me", "entry-1:slot-1", inbox=cast(Any, inbox))

    folder = home / "workflows" / "runs" / "entry-1:slot-1"
    assert inbox.sent[0][0][2] == f"workflow run entry-1:slot-1 failed, run folder {folder}"


def test_a_second_notice_for_the_same_run_is_dropped(home: Path) -> None:
    finished_run(home, "entry-1:slot-1", "failed")
    inbox = Inbox(duplicate=True)

    assert scheduled.notify("me", "entry-1:slot-1", inbox=cast(Any, inbox)) is None

    assert len(inbox.sent) == 1


def test_notify_connects_to_the_inbox_by_default(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    inbox = Inbox()
    monkeypatch.setattr(inbox_store, "connect", lambda: inbox)

    scheduled.notify("me", "entry-1:slot-1")

    assert inbox.sent[0][1] == {"item_id": "entry-1:slot-1:done"}


def test_main_runs_the_workflow_then_notifies_the_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(scheduled.cli, "main", lambda argv: calls.append(("run", argv)) or 1)
    monkeypatch.setattr(scheduled, "notify", lambda owner, run_id: calls.append(("notify", [owner, run_id])))

    assert scheduled.main(["me", "tiny", "/case.md", "entry-1:slot-1"]) == 1

    assert calls == [
        ("run", ["run", "tiny", "--case", "/case.md", "--run-id", "entry-1:slot-1"]),
        ("notify", ["me", "entry-1:slot-1"]),
    ]


def test_main_notifies_the_owner_when_the_run_crashes(monkeypatch: pytest.MonkeyPatch) -> None:
    notified: list[tuple[str, str]] = []

    def crash(argv: list[str]) -> int:
        raise RuntimeError("runner died")

    monkeypatch.setattr(scheduled.cli, "main", crash)
    monkeypatch.setattr(scheduled, "notify", lambda owner, run_id: notified.append((owner, run_id)))

    with pytest.raises(RuntimeError):
        scheduled.main(["me", "tiny", "/case.md", "entry-1:slot-1"])

    assert notified == [("me", "entry-1:slot-1")]
