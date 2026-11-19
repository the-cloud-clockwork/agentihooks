"""Focus and unfreeze answer with the claim gate verdict on every task under their target, and one read lists every
focus with those verdicts and the minutes it has gone without a claim."""

import io
import json
import urllib.error
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.doctor import loop, priming
from scripts.swarm import cli
from scripts.swarm.health import focus_check
from scripts.swarm.ledger_client import LedgerClient, LedgerOffline
from scripts.swarm.store import RedisStore, SwarmConfig, SwarmError
from scripts.swarm_ledger import ledger, ledger_freezes
from scripts.swarm_ledger.api import routes
from tests.swarm.test_freeze import doc, record, task
from tests.swarm.test_swarm_freeze_cli import acting, swarm  # noqa: F401

pytestmark = pytest.mark.unit

MINUTE = 60_000


def ledger_doc(*records: dict[str, Any]) -> dict[str, Any]:
    return doc(
        *records,
        tasks=[
            task("t1"),
            task("t2", depends_on=["t3"]),
            task("t3", phase="p2"),
            task("t4", state="done"),
            task("t5"),
            task("t6", state="claimed", claimed_by="engineer@one"),
            task("t7", out_of_scope=True),
            task("t8", state="pr"),
        ],
    )


def verdicts(found: dict[str, Any] | None) -> dict[str, str]:
    assert found is not None
    return {row["id"]: row["verdict"] for row in found["tasks"]}


def one(found: dict[str, Any], path: str) -> dict[str, Any] | None:
    answer = focus_check.read(found, path, "", 0)
    assert not isinstance(answer, list)
    return answer


def every(found: dict[str, Any], now_ms: int) -> list[dict[str, Any]]:
    answer = focus_check.read(found, "focus", "", now_ms)
    assert isinstance(answer, list)
    return answer


@pytest.mark.feature("swarm-focus-verdict")
def test_every_task_under_a_focus_gets_its_claim_gate_verdict() -> None:
    found = one(ledger_doc(record("phases/p1", "focus"), record("tasks/t5")), "focus/phases/p1")
    assert found is not None
    assert verdicts(found) == {
        "t1": "claimable",
        "t2": "it waits on task t3",
        "t5": "the freeze on task t5 holds it",
        "t6": "it is claimed by engineer@one",
        "t7": "it is out of scope",
        "t8": "it is in a pull request",
    }
    assert (found["target"], found["claimable"], found["done"]) == ("phases/p1", 1, ["t4"])
    assert focus_check.lines("focus", "phases/p1", found)[-1] == "  t4  it is already done"


@pytest.mark.feature("swarm-focus-verdict")
def test_a_task_outside_the_focus_is_named_as_held_by_it() -> None:
    found = one(ledger_doc(record("tasks/t1", "focus")), "focus/tasks/t3")
    assert found is not None
    assert verdicts(found) == {"t3": "a focus elsewhere holds it"}


@pytest.mark.feature("swarm-focus-verdict")
def test_a_target_that_does_not_exist_has_no_verdict() -> None:
    assert one(ledger_doc(), "focus/tasks/nope") is None
    assert focus_check.lines("unfreeze", "tasks/nope", None) == ["unfreeze tasks/nope: no such target"]


@pytest.mark.feature("swarm-focus-verdict")
def test_a_focus_with_nothing_claimable_prints_a_loud_head() -> None:
    found = one(ledger_doc(record("tasks/t2", "focus")), "focus/tasks/t2")
    assert focus_check.lines("focus", "tasks/t2", found) == [
        "focus tasks/t2: NOTHING CLAIMABLE, no agent will start work from it; 1 open, 0 done",
        "  t2  eng  it waits on task t3",
    ]
    found = one(ledger_doc(record("tasks/t1", "focus")), "focus/tasks/t1")
    assert focus_check.lines("focus", "tasks/t1", found)[0] == "focus tasks/t1: 1 claimable, 1 open, 0 done"


@pytest.mark.feature("swarm-focus-verdict")
@pytest.mark.parametrize("verb", ["focus", "unfreeze"])
def test_focus_and_unfreeze_print_the_verdict_after_the_write(
    swarm: tuple[RedisStore, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    verb: str,
) -> None:
    _, fake = swarm
    acting(monkeypatch)
    asked: list[tuple[str, str]] = []
    answer = {"target": "tasks/t2", "claimable": 0, "done": [], "tasks": [{"id": "t2", "lane": "eng", "verdict": "x"}]}
    fake.focus_verdicts = lambda slug, target: asked.append((slug, target)) or answer
    assert cli.main(["demo", verb, "tasks/t2"]) == 0
    out, err = capsys.readouterr()
    assert json.loads(out) == {verb: "tasks/t2", "by": "operator"}
    assert err.splitlines() == [
        f"{verb} tasks/t2: NOTHING CLAIMABLE, no agent will start work from it; 1 open, 0 done",
        "  t2  eng  x",
    ]
    assert [entry[:3] for entry in fake.freezes] == [("demo", verb, "tasks/t2")]
    assert asked == [("demo", "tasks/t2")]


@pytest.mark.feature("swarm-focus-verdict")
def test_freeze_prints_no_verdict(
    swarm: tuple[RedisStore, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, fake = swarm
    acting(monkeypatch)
    fake.focus_verdicts = lambda slug, target: pytest.fail("freeze asked for a verdict")
    assert cli.main(["demo", "freeze", "tasks/t2"]) == 0
    assert capsys.readouterr().err == ""


@pytest.mark.feature("swarm-focus-verdict")
@pytest.mark.parametrize("failure", ["offline", "server error"])
def test_a_written_focus_says_so_when_the_ledger_gives_no_verdict(
    swarm: tuple[RedisStore, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    _, fake = swarm
    acting(monkeypatch)

    def unavailable(slug: str, target: str) -> None:
        raise LedgerOffline(f"ledger {slug}: offline") if failure == "offline" else missing(500, "internal")

    fake.focus_verdicts = unavailable
    assert cli.main(["demo", "focus", "tasks/t2"]) == 0
    out, err = capsys.readouterr()
    assert json.loads(out) == {"focus": "tasks/t2", "by": "operator"}
    assert err.startswith("focus tasks/t2: written, but the ledger gave no verdict: ")
    assert [entry[:3] for entry in fake.freezes] == [("demo", "focus", "tasks/t2")]


@pytest.mark.feature("swarm-focus-read")
def test_the_fix_phase_comes_from_the_swarm_config_and_is_empty_without_a_swarm() -> None:
    def services(config: Any) -> Any:
        def read(slug: str) -> SwarmConfig:
            if config is None:
                raise SwarmError(f"no swarm {slug}")
            return config

        return SimpleNamespace(swarm=SimpleNamespace(store=lambda: SimpleNamespace(config=read)))

    assert routes.fix_phase(services(SwarmConfig("demo", "/repo", template=priming.TEMPLATE)), "demo") == loop.FIX_PHASE
    assert routes.fix_phase(services(SwarmConfig("demo", "/repo")), "demo") == ""
    assert routes.fix_phase(services(None), "demo") == ""


class Context:
    def __init__(self) -> None:
        self.refused: list[str] = []
        self.recorded: list[tuple[Any, ...]] = []

    def record(self, *fields: Any, **named: Any) -> None:
        self.recorded.append(fields)

    def stamp(self, *fields: Any) -> None:
        self.recorded.append(fields)


@pytest.mark.feature("swarm-focus-verdict")
def test_unfreeze_of_a_target_that_does_not_exist_is_refused() -> None:
    found, ctx = ledger_doc(record("tasks/gone", "focus")), Context()
    op = {"op": "freeze_clear", "id": "c1", "target": "tasks/nope"}
    assert ledger_freezes._clear(found, op, ctx, "operator") is False
    assert ctx.refused == ["freeze_clear refused: target tasks/nope does not exist"]
    for target in ("tasks/gone", "tasks/t1", "lane:eng"):
        assert ledger_freezes._clear(found, {**op, "target": target}, ctx, "operator") is True
    assert ctx.refused == ["freeze_clear refused: target tasks/nope does not exist"]
    assert found["freezes"] == []


@pytest.mark.feature("swarm-focus-read")
def test_the_focus_read_lists_every_focus_with_verdicts_and_unclaimed_minutes() -> None:
    found = ledger_doc(
        {**record("tasks/t1", "focus"), "at": MINUTE, "reason": "ship it"},
        record("tasks/t5"),
        record("tasks/t4", "focus"),
        record("tasks/t6", "focus"),
    )
    assert every(found, 6 * MINUTE) == [
        {
            "target": "tasks/t1",
            "claimable": 1,
            "done": [],
            "tasks": [{"id": "t1", "lane": "eng", "state": "open", "verdict": "claimable"}],
            "by": "operator",
            "at": MINUTE,
            "reason": "ship it",
            "unclaimed_minutes": 5,
        },
        {
            "target": "tasks/t4",
            "claimable": 0,
            "done": ["t4"],
            "tasks": [],
            "by": "operator",
            "at": 1,
            "reason": "",
            "unclaimed_minutes": None,
        },
        {
            "target": "tasks/t6",
            "claimable": 0,
            "done": [],
            "tasks": [{"id": "t6", "lane": "eng", "state": "claimed", "verdict": "it is claimed by engineer@one"}],
            "by": "operator",
            "at": 1,
            "reason": "",
            "unclaimed_minutes": None,
        },
    ]


@pytest.mark.feature("swarm-focus-read")
def test_a_lane_focus_covers_every_task_of_its_lane() -> None:
    found = doc(record("lane:ci", "focus"), tasks=[task("t1"), task("c1", lane="ci")])
    [row] = every(found, 0)
    assert [task_row["id"] for task_row in row["tasks"]] == ["c1"]
    assert one(found, "focus/lane:ci") == {key: row[key] for key in ("target", "claimable", "done", "tasks")}


@pytest.mark.feature("swarm-focus-read")
def test_ledger_get_takes_the_focus_read() -> None:
    assert ledger._ledger_item("focus") == "focus"


def missing(code: int, error: str) -> urllib.error.HTTPError:
    body = json.dumps({"error": {"code": error, "message": "No such target"}}).encode()
    return urllib.error.HTTPError("http://ledger/focus", code, "missing", {}, io.BytesIO(body))  # type: ignore[arg-type]


@pytest.mark.feature("swarm-focus-read")
def test_the_client_reads_one_target_and_answers_none_for_a_missing_one(monkeypatch: pytest.MonkeyPatch) -> None:
    paths: list[str] = []

    def resource(self: LedgerClient, slug: str, path: str, collection: bool = False) -> dict[str, Any]:
        paths.append(path)
        if path.endswith("nope"):
            raise missing(404, "resource_missing")
        return {"target": "tasks/t1"}

    monkeypatch.setattr(LedgerClient, "_resource", resource)
    assert LedgerClient().focus_verdicts("demo", "tasks/t1") == {"target": "tasks/t1"}
    assert LedgerClient().focus_verdicts("demo", "tasks/nope") is None
    assert paths == ["focus/tasks/t1", "focus/tasks/nope"]
