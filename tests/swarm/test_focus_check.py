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
from scripts.swarm_ledger.api import resources, routes
from scripts.swarm_ledger.api.errors import APIError
from tests.swarm.test_freeze import ELSEWHERE, doc, record, task
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
    assert one(ledger_doc(record("tasks/t1", "focus")), "focus/tasks/nope") is None
    found = one(ledger_doc(record("tasks/gone", "focus")), "focus/tasks/gone")
    assert found is not None and found["tasks"] == []
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


class OneTarget:
    asked: list[str] = []

    def focus_verdicts(self, slug: str, target: str) -> dict[str, Any]:
        self.asked.append(slug)
        return {"target": target, "claimable": 1, "done": [], "tasks": []}


@pytest.mark.feature("swarm-focus-verdict")
def test_checked_runs_the_command_then_writes_the_verdict_on_its_target(capsys: pytest.CaptureFixture[str]) -> None:
    ran: list[tuple[Any, str]] = []
    store, OneTarget.asked = object(), []
    run = focus_check.checked(lambda given, args: ran.append((given, args.target)), OneTarget)
    run(store, SimpleNamespace(slug="demo", command="focus", target="tasks/t1"))
    assert ran == [(store, "tasks/t1")]
    assert OneTarget.asked == ["demo"]
    assert capsys.readouterr().err == "focus tasks/t1: 1 claimable, 0 open, 0 done\n"


@pytest.mark.feature("swarm-focus-read")
def test_the_fix_phase_comes_from_the_swarm_config_and_is_empty_without_a_swarm() -> None:
    slugs: list[str] = []

    def services(config: Any) -> Any:
        def read(slug: str) -> SwarmConfig:
            slugs.append(slug)
            if config is None:
                raise SwarmError(f"no swarm {slug}")
            return config

        return SimpleNamespace(swarm=SimpleNamespace(store=lambda: SimpleNamespace(config=read)))

    doctor = SwarmConfig("demo", "/repo", max_eng=1, max_ci=1, template=priming.TEMPLATE)
    assert routes.fix_phase(services(doctor), "demo") == loop.FIX_PHASE
    assert routes.fix_phase(services(SwarmConfig("demo", "/repo", max_eng=1, max_ci=1)), "demo") == ""
    assert routes.fix_phase(services(None), "demo") == ""
    assert slugs == ["demo", "demo", "demo"]


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


@pytest.mark.feature("swarm-focus-read")
@pytest.mark.parametrize(("code", "error"), [(404, "internal"), (500, "resource_missing")])
def test_the_client_raises_a_failure_other_than_a_missing_target(
    monkeypatch: pytest.MonkeyPatch, code: int, error: str
) -> None:
    slugs: list[str] = []

    def resource(self: LedgerClient, slug: str, path: str, collection: bool = False) -> dict[str, Any]:
        slugs.append(slug)
        raise missing(code, error)

    monkeypatch.setattr(LedgerClient, "_resource", resource)
    with pytest.raises(urllib.error.HTTPError) as raised:
        LedgerClient().focus_verdicts("demo", "tasks/t1")
    assert raised.value.code == code
    assert slugs == ["demo"]


@pytest.mark.feature("swarm-focus-verdict")
def test_a_task_with_no_state_lane_or_claimant_reads_as_open_unlaned_and_claimed_by_an_agent() -> None:
    bare = [
        {"id": "t1", "lane": "eng", "phase": "p1"},
        {"id": "t6", "state": "claimed", "phase": "p1"},
        {"id": "t9", "lane": "eng", "state": "parked", "phase": "p1"},
    ]
    found = one(doc(record("phases/p1", "focus"), tasks=bare), "focus/phases/p1")
    assert found is not None
    assert found["tasks"] == [
        {"id": "t1", "lane": "eng", "state": "open", "verdict": "claimable"},
        {"id": "t6", "lane": "", "state": "claimed", "verdict": "it is claimed by an agent"},
        {"id": "t9", "lane": "eng", "state": "parked", "verdict": "it is parked"},
    ]


@pytest.mark.feature("swarm-focus-verdict")
def test_the_fix_phase_passes_a_doctor_fix_through_a_focus_elsewhere() -> None:
    found = doc(record("lane:ci", "focus"), tasks=[task("t1", phase=loop.FIX_PHASE), ELSEWHERE])
    fixed = focus_check.read(found, "focus/lane:eng", loop.FIX_PHASE, 0)
    plain = focus_check.read(found, "focus/lane:eng", "", 0)
    assert isinstance(fixed, dict) and isinstance(plain, dict)
    assert verdicts(fixed) == {"t1": "claimable"}
    assert verdicts(plain) == {"t1": "a focus elsewhere holds it"}


@pytest.mark.feature("ledger-focus-task-pages")
def test_task_rows_passes_the_fix_phase_through_a_focus_elsewhere() -> None:
    found = doc(record("lane:ci", "focus"), tasks=[task("t1", phase=loop.FIX_PHASE), ELSEWHERE])
    fixed = focus_check.task_rows(found, "focus/lane:eng/tasks", loop.FIX_PHASE)
    plain = focus_check.task_rows(found, "focus/lane:eng/tasks", "")
    assert fixed is not None and plain is not None
    assert {row["id"]: row["verdict"] for row in fixed} == {"t1": "claimable"}
    assert {row["id"]: row["verdict"] for row in plain} == {"t1": "a focus elsewhere holds it"}


@pytest.mark.feature("ledger-focus-task-pages")
def test_task_rows_is_none_under_no_target_or_with_no_tasks_suffix_and_lists_a_node_named_tasks_one_down() -> None:
    found = ledger_doc(record("phases/p1", "focus"))
    assert focus_check.task_rows(found, "focus/phases/none/tasks", "") is None
    assert focus_check.task_rows(found, "focus/phases/p1", "") is None
    tasked = doc(tasks=[task("tasks"), task("t1")])
    assert focus_check.task_rows(tasked, "focus/tasks/tasks", "") is None
    under = focus_check.task_rows(tasked, "focus/tasks/tasks/tasks", "")
    assert under is not None and [row["id"] for row in under] == ["tasks"]


@pytest.mark.feature("swarm-focus-verdict")
def test_a_selector_target_is_known_only_with_a_value_and_split_at_its_first_colon() -> None:
    bare = ledger_doc()
    seen = focus_check.view(bare, "")
    assert one(bare, "focus/lane:eng") is not None
    assert one(bare, "focus/lane:") is None
    assert focus_check.known("lane:eng:x", seen) is True
    assert focus_check.under("nope:x", seen) == []
    split = doc(tasks=[task("t1", lane="eng:x"), task("t2")])
    assert [row["id"] for row in focus_check.under("lane:eng:x", focus_check.view(split, ""))] == ["t1"]


@pytest.mark.feature("swarm-focus-read")
def test_unclaimed_minutes_count_whole_minutes_and_never_go_below_zero() -> None:
    found = {"tasks": [{"state": "open"}]}
    assert focus_check.unclaimed({"at": 0}, found, 90_000) == 1
    assert focus_check.unclaimed({"at": 0}, found, 30_000) == 0
    assert focus_check.unclaimed({"at": 90_000}, found, 0) == 0


class Current:
    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state
        self.asked: list[str] = []

    def current(self, slug: str) -> Any:
        self.asked.append(slug)
        return SimpleNamespace(state=self.state)


@pytest.mark.feature("swarm-focus-read")
@pytest.mark.parametrize("path", ["focus", "focus/tasks/t1"])
def test_the_focus_route_reads_the_slug_state_fix_phase_and_clock(monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    state: dict[str, Any] = {"tasks": []}
    repository = Current(state)
    services = SimpleNamespace(repository=repository)
    fixes: list[tuple[Any, str]] = []
    reads: list[tuple[Any, ...]] = []
    found: Any = [{"target": "tasks/t1"}] if path == "focus" else {"target": "tasks/t1"}

    def fix_phase(given: Any, slug: str) -> str:
        fixes.append((given, slug))
        return "p9"

    def read(doc: dict[str, Any], asked: str, fix: str, now_ms: int) -> Any:
        reads.append((doc, asked, fix, now_ms))
        return found

    monkeypatch.setattr(routes, "time", SimpleNamespace(time=lambda: 12.5))
    monkeypatch.setattr(routes, "fix_phase", fix_phase)
    monkeypatch.setattr(focus_check, "read", read)
    reply = routes.focus_read(services, "demo", path)  # type: ignore[arg-type]
    assert repository.asked == ["demo"]
    assert fixes == [(services, "demo")]
    assert reads == [(state, path, "p9", 12_500)]
    assert reply["data"] == found
    assert reply["revision"] == resources.revision(found)
    assert ("next_cursor" in reply) is (path == "focus")


@pytest.mark.feature("swarm-focus-read")
def test_the_focus_route_answers_404_for_a_target_that_does_not_exist(monkeypatch: pytest.MonkeyPatch) -> None:
    services = SimpleNamespace(repository=Current({"tasks": []}))
    monkeypatch.setattr(routes, "fix_phase", lambda given, slug: "")
    monkeypatch.setattr(focus_check, "read", lambda doc, asked, fix, now_ms: None)
    with pytest.raises(APIError) as raised:
        routes.focus_read(services, "demo", "focus/tasks/nope")  # type: ignore[arg-type]
    assert (raised.value.status, raised.value.code, str(raised.value)) == (404, "resource_missing", "No such target")


@pytest.mark.feature("swarm-focus-read")
def test_a_ledger_with_no_tasks_reads_each_focus_with_no_tasks() -> None:
    bare = {key: value for key, value in doc(record("phases/p1", "focus")).items() if key != "tasks"}
    [found] = every(bare, 0)
    assert (found["target"], found["claimable"], found["done"], found["tasks"]) == ("phases/p1", 0, [], [])


@pytest.mark.feature("swarm-focus-read")
def test_the_ledger_read_sends_a_focus_path_to_the_focus_read_with_its_slug(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[tuple[Any, str, str]] = []

    def focus_read(services: Any, slug: str, path: str) -> dict[str, Any]:
        asked.append((services, slug, path))
        return {"data": []}

    services = SimpleNamespace()
    monkeypatch.setattr(routes, "focus_read", focus_read)
    assert routes.ledger_read(services, "demo", "focus/tasks/t1", {}) == {"data": []}  # type: ignore[arg-type]
    assert asked == [(services, "demo", "focus/tasks/t1")]


@pytest.mark.feature("ledger-focus-task-pages")
def test_the_focus_tasks_route_hands_the_fix_phase_of_the_slug_to_task_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    state: dict[str, Any] = {"tasks": []}
    services = SimpleNamespace(repository=Current(state))
    fixes: list[tuple[Any, str]] = []
    reads: list[tuple[Any, str, str]] = []
    rows = [{"id": "t1", "lane": "eng", "state": "open", "verdict": "claimable"}]

    def fix_phase(given: Any, slug: str) -> str:
        fixes.append((given, slug))
        return "p9"

    def task_rows(given_doc: dict[str, Any], path: str, fix: str) -> list[dict[str, Any]]:
        reads.append((given_doc, path, fix))
        return rows

    monkeypatch.setattr(routes, "fix_phase", fix_phase)
    monkeypatch.setattr(focus_check, "task_rows", task_rows)
    reply = routes.focus_tasks(services, "demo", "focus/lane:eng/tasks", {})  # type: ignore[arg-type]
    assert fixes == [(services, "demo")]
    assert reads == [(state, "focus/lane:eng/tasks", "p9")]
    assert reply is not None and [row["id"] for row in reply["data"]] == ["t1"]
