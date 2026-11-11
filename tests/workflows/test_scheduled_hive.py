"""A hive run reports its result through the run receipt and the sending host posts the owner's notice."""

import fnmatch
import json
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from hooks import config
from scripts.workflows import scheduled, store

pytestmark = pytest.mark.unit

RUN = "run-1"
HIVE = "hive-a"


class Spawn:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **options: object) -> None:
        self.calls.append(argv)


class Inbox:
    def __init__(self) -> None:
        self.sent: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def send(self, *args: str, **options: object) -> str:
        self.sent.append((args, options))
        return "item"

    def get(self, item_id: str) -> str:
        return f"existing {item_id}"


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    root = tmp_path / "home"
    monkeypatch.setattr(config, "AGENTIHOOKS_HOME", str(root))
    return root


@pytest.fixture
def redis() -> Any:
    import fakeredis

    return fakeredis.FakeRedis(decode_responses=True)


def finished_run(home: Path, state: dict[str, str]) -> Path:
    folder = home / "workflows" / "runs" / RUN
    (folder / store.OUTPUTS_DIR).mkdir(parents=True)
    store.save(folder, state)
    (folder / store.OUTPUTS_DIR / "build.md").write_text("built")
    return folder


def run_order() -> Any:
    from scripts.hive.run_orders import RunOrder

    return RunOrder(RUN, "tiny", "case", "github.com/owner/project", "me", (), HIVE, 2_000_000)


@pytest.mark.feature("hive-run-result")
def test_a_hive_start_passes_the_hive_to_the_detached_run(home: Path) -> None:
    spawn = Spawn()

    assert scheduled.start("tiny", "/case.md", "/project", RUN, "me", popen=spawn, hive=HIVE) is True

    assert spawn.calls == [
        [sys.executable, "-P", "-m", "scripts.workflows.scheduled", "me", "tiny", "/case.md", RUN, HIVE]
    ]


@pytest.mark.feature("hive-run-result")
def test_main_on_a_hive_reports_to_the_receipt_and_posts_no_notice(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(scheduled.cli, "main", lambda argv: 0)
    monkeypatch.setattr(scheduled, "notify", lambda owner, run_id: calls.append(("notify", owner, run_id)))
    monkeypatch.setattr(scheduled, "report", lambda hive, run_id: calls.append(("report", hive, run_id)))

    assert scheduled.main(["me", "tiny", "/case.md", RUN, HIVE]) == 0

    assert calls == [("report", HIVE, RUN)]


@pytest.mark.feature("hive-run-result")
def test_report_writes_the_final_status_and_run_folder_files_into_the_receipt(home: Path, redis: Any) -> None:
    finished_run(home, {"status": "passed"})

    from scripts.hive.run_orders import RunOrders

    scheduled.report(HIVE, RUN, redis)

    receipt = RunOrders(redis).receipt(run_order())
    assert scheduled.receipt(HIVE, RUN) == receipt
    result = json.loads(redis.hget(receipt, scheduled.RESULT))
    assert result["status"] == "passed"
    assert result["hive"] == HIVE
    assert result["files"]["outputs/build.md"] == "built"
    assert json.loads(result["files"][store.STATE_FILE]) == {"status": "passed"}


@pytest.mark.feature("hive-run-result")
def test_report_keeps_the_first_result_for_a_run_id(home: Path, redis: Any) -> None:
    folder = finished_run(home, {"status": "passed"})
    scheduled.report(HIVE, RUN, redis)
    store.save(folder, {"status": "failed"})

    scheduled.report(HIVE, RUN, redis)

    assert json.loads(redis.hget(scheduled.receipt(HIVE, RUN), scheduled.RESULT))["status"] == "passed"


@pytest.mark.feature("hive-run-result")
def test_collect_restores_the_run_so_workflow_status_reads_the_same_result(
    home: Path, tmp_path: Path, redis: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    finished_run(home, {"status": "blocked"})
    scheduled.report(HIVE, RUN, redis)
    monkeypatch.setattr(config, "AGENTIHOOKS_HOME", str(tmp_path / "scheduler"))
    inbox = Inbox()

    assert scheduled.collect(redis, HIVE, RUN, "me", inbox=cast(Any, inbox)) is True

    folder = tmp_path / "scheduler" / "workflows" / "runs" / RUN
    assert scheduled.cli.status(scheduled.cli.parser().parse_args(["status", RUN])) == {"status": "blocked"}
    assert (folder / store.OUTPUTS_DIR / "build.md").read_text() == "built"
    assert inbox.sent == [
        (
            ("scheduler", "me", f"workflow run {RUN} blocked on hive {HIVE}, run folder {folder}"),
            {"ref": "", "fyi": False, "task": "", "item_id": f"{RUN}:done"},
        )
    ]


@pytest.mark.feature("hive-run-result")
def test_collect_posts_one_notice_per_run_id(home: Path, redis: Any) -> None:
    finished_run(home, {"status": "passed"})
    scheduled.report(HIVE, RUN, redis)
    inbox = Inbox()

    assert scheduled.collect(redis, HIVE, RUN, "me", inbox=cast(Any, inbox)) is True
    assert scheduled.collect(redis, HIVE, RUN, "me", inbox=cast(Any, inbox)) is False

    assert len(inbox.sent) == 1


@pytest.mark.feature("hive-run-result")
def test_collect_waits_while_the_hive_has_reported_nothing(home: Path, redis: Any) -> None:
    inbox = Inbox()

    assert scheduled.collect(redis, HIVE, RUN, "me", inbox=cast(Any, inbox)) is False

    assert inbox.sent == []
    assert not (home / "workflows" / "runs" / RUN).exists()


@pytest.mark.feature("hive-run-result")
@pytest.mark.parametrize("name", ["../escape", "/abs", "outputs/../../escape"])
def test_restore_refuses_a_file_outside_the_run_folder(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError) as raised:
        store.restore(tmp_path / "run", {name: "x"})

    assert str(raised.value) == f"run file {name} leaves the run folder"
    assert not (tmp_path / "escape").exists()


@pytest.mark.feature("hive-run-result")
def test_a_hive_member_may_write_its_run_receipt_but_not_the_owners_inbox() -> None:
    from scripts.hive import auth

    grants = [pattern[1:] for pattern in auth._acl_keys(HIVE) if pattern.startswith("~")]
    inbox = f"{auth.ROOT}:inbox:owner:me"

    assert any(fnmatch.fnmatchcase(scheduled.receipt(HIVE, RUN), grant) for grant in grants)
    assert not any(fnmatch.fnmatchcase(inbox, grant) for grant in grants)
