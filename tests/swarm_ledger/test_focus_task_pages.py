"""The task list of one focus target read in pages: a target one reply cannot hold, the cursor between two pages,
the names that stay a target of their own, the restart of a list that changed, the command line forms and what each
read past the reply limit names."""

import json
import sys
import urllib.error
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from scripts.feature_checks import focus_task_pages
from scripts.swarm_ledger import ledger
from scripts.swarm_ledger.api import client as api_client
from scripts.swarm_ledger.api import resources, routes
from scripts.swarm_ledger.api.errors import APIError
from scripts.swarm_ledger.repository import sqlite as store
from tests.swarm.test_freeze import doc, record, task
from tests.swarm_ledger.api_fakes import as_services
from tests.swarm_ledger.test_hierarchy_pages import Served, backoff

pytestmark = pytest.mark.unit

SLUG = "focus-pages"
LIST = "focus/phases/p1/tasks"
READ = f"agentihooks api --slug {SLUG} get"
ROW = {"id", "lane", "state", "verdict", "revision"}


def kept(folder: Path, tasks: list[dict[str, Any]], *records: dict[str, Any]) -> Any:
    folder.mkdir(exist_ok=True)
    repo = store.SQLiteLedgerRepository(folder / store.DATABASE)
    repo.import_document(SLUG, {**doc(*records, tasks=tasks), "_meta": {"rev": 1}})
    return repo


def served(repo: Any) -> Any:
    return as_services(SimpleNamespace(repository=repo))


def no_fix(services: Any, slug: str) -> str:
    return ""


def plain(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key != "revision"}


def refusal(services: Any, path: str, query: dict[str, Any]) -> tuple[int, str, str]:
    with pytest.raises(APIError) as refused:
        routes.ledger_read(services, SLUG, path, query)
    return refused.value.status, refused.value.code, str(refused.value)


@pytest.fixture
def pauses(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    seen: list[Any] = []
    monkeypatch.setattr(api_client.time, "sleep", seen.append)
    monkeypatch.setattr(api_client.random, "uniform", lambda low, high: (low, high))
    return seen


@pytest.fixture(autouse=True)
def unfixed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(routes, "fix_phase", no_fix)


@pytest.mark.feature("ledger-focus-task-pages")
def test_a_target_one_reply_cannot_hold_is_read_whole_through_its_task_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = kept(tmp_path, [*(task(f"t{number}") for number in range(250)), task("t-done", state="done")])
    whole = routes.ledger_read(served(repo), SLUG, "focus/phases/p1", {})["data"]
    assert len(whole["tasks"]) == 250 and whole["done"] == ["t-done"]
    monkeypatch.setattr(resources, "MAX_REPLY", resources.reply_size({"data": whole}) - 1)
    assert refusal(served(repo), "focus/phases/p1", {})[:2] == (413, "resource_too_large")
    reader = Served(repo)
    rows = reader.collection(SLUG, LIST)
    assert [plain(row) for row in rows] == whole["tasks"]
    assert [row["revision"] for row in rows] == [resources.revision(row) for row in whole["tasks"]]
    assert all(row.keys() == ROW for row in rows)
    assert [urlsplit(path).path for path in reader.paths] == [LIST, LIST, LIST]


@pytest.mark.feature("ledger-focus-task-pages")
def test_a_page_of_the_task_list_carries_the_cursor_to_the_next_page(tmp_path: Path) -> None:
    services = served(kept(tmp_path, [task(f"t{number}") for number in range(5)]))
    whole = routes.ledger_read(services, SLUG, "focus/phases/p1", {})["data"]["tasks"]
    first = routes.ledger_read(services, SLUG, LIST, {"limit": 2})
    assert [row["id"] for row in first["data"]] == ["t0", "t1"]
    assert first["revision"] == resources.revision(whole)
    assert first["next_cursor"] == f"{first['revision']}:2"
    second = routes.ledger_read(services, SLUG, LIST, {"limit": 2, "cursor": first["next_cursor"]})
    assert [row["id"] for row in second["data"]] == ["t2", "t3"]
    assert second["revision"] == first["revision"]
    last = routes.ledger_read(services, SLUG, LIST, {"limit": 2, "cursor": second["next_cursor"]})
    assert [row["id"] for row in last["data"]] == ["t4"]
    assert last["next_cursor"] is None
    assert first.keys() == second.keys() == last.keys() == {"data", "revision", "next_cursor"}


@pytest.mark.feature("ledger-focus-task-pages")
def test_a_cursor_of_a_task_list_that_changed_is_refused_as_a_conflict(tmp_path: Path) -> None:
    before = served(kept(tmp_path / "before", [task(f"t{number}") for number in range(5)]))
    changed = [task(f"t{number}", state="claimed" if number == 4 else "open") for number in range(5)]
    after = served(kept(tmp_path / "after", changed))
    cursor = routes.ledger_read(before, SLUG, LIST, {"limit": 2})["next_cursor"]
    assert refusal(after, LIST, {"limit": 2, "cursor": cursor}) == (
        409,
        "revision_conflict",
        "Collection changed; restart pagination",
    )


@pytest.mark.feature("ledger-focus-task-pages")
def test_a_task_list_holds_the_unfinished_tasks_of_its_target_and_no_done_one(tmp_path: Path) -> None:
    tasks = [
        task("t1"),
        task("t2", state="claimed", claimed_by="engineer@one"),
        task("t3", state="done"),
        task("t4", out_of_scope=True),
        task("t9", phase="p2", lane="ci"),
    ]
    services = served(kept(tmp_path, tasks))
    listed = routes.ledger_read(services, SLUG, LIST, {})
    assert [(row["id"], row["lane"], row["state"], row["verdict"]) for row in listed["data"]] == [
        ("t1", "eng", "open", "claimable"),
        ("t2", "eng", "claimed", "it is claimed by engineer@one"),
        ("t4", "eng", "open", "it is out of scope"),
    ]
    assert listed["next_cursor"] is None
    by_lane = routes.ledger_read(services, SLUG, "focus/lane:ci/tasks", {})
    assert [row["id"] for row in by_lane["data"]] == ["t9"]
    under_plan = routes.ledger_read(services, SLUG, "focus/plans/b/tasks", {})
    assert [row["id"] for row in under_plan["data"]] == ["t9"]
    alone = routes.ledger_read(services, SLUG, "focus/tasks/t3/tasks", {})
    assert alone == {"data": [], "revision": resources.revision([]), "next_cursor": None}


@pytest.mark.feature("ledger-focus-task-pages")
@pytest.mark.parametrize(
    "path",
    ["focus/phases/none/tasks", "focus/tasks", "focus/phases/tasks", "focus/t1/tasks", "focus//tasks", "focus/tasks/"],
)
def test_a_task_list_under_no_target_answers_404(tmp_path: Path, path: str) -> None:
    services = served(kept(tmp_path, [task("t1")]))
    assert refusal(services, path, {}) == (404, "resource_missing", "No such target")


@pytest.mark.feature("ledger-focus-task-pages")
def test_a_node_whose_id_is_tasks_keeps_its_one_reply_read_and_lists_its_tasks_one_name_down(tmp_path: Path) -> None:
    services = served(kept(tmp_path, [task("tasks"), task("t1")]))
    node = routes.ledger_read(services, SLUG, "focus/tasks/tasks", {})
    assert node["data"]["target"] == "tasks/tasks" and "next_cursor" not in node
    assert [row["id"] for row in node["data"]["tasks"]] == ["tasks"]
    under = routes.ledger_read(services, SLUG, "focus/tasks/tasks/tasks", {})
    assert [row["id"] for row in under["data"]] == ["tasks"] and under["next_cursor"] is None


@pytest.mark.feature("ledger-focus-task-pages")
def test_a_task_list_that_changes_between_two_pages_is_read_again_from_its_first_page(
    tmp_path: Path, pauses: list[Any]
) -> None:
    before = kept(tmp_path / "before", [task(f"t{number}") for number in range(150)])
    changed = [task(f"t{number}", state="claimed" if number == 0 else "open") for number in range(150)]
    after = kept(tmp_path / "after", changed)
    reader = Served(before, after, after, after)
    rows = reader.collection(SLUG, LIST)
    assert [row["id"] for row in rows] == [f"t{number}" for number in range(150)]
    assert rows[0]["state"] == "claimed"
    assert [urlsplit(path).query.startswith("limit=100&cursor=") for path in reader.paths] == [
        False,
        True,
        False,
        True,
    ]
    assert [urlsplit(path).path for path in reader.paths] == [LIST] * 4
    assert len(pauses) == 1 and backoff(0, pauses[0])


@pytest.mark.feature("ledger-focus-task-pages")
def test_a_task_list_that_keeps_changing_gives_up_with_the_conflict(tmp_path: Path, pauses: list[Any]) -> None:
    one = kept(tmp_path / "one", [task(f"t{number}") for number in range(150)])
    other = kept(tmp_path / "other", [task(f"t{number}", state="pr") for number in range(150)])
    reader = Served(one, other)
    with pytest.raises(urllib.error.HTTPError) as caught:
        reader.collection(SLUG, LIST)
    assert caught.value.code == 409
    assert [urlsplit(path).path for path in reader.paths] == [LIST] * (2 * (api_client.RETRIES + 1))
    assert len(pauses) == api_client.RETRIES and all(backoff(number, pause) for number, pause in enumerate(pauses))


@pytest.mark.feature("ledger-focus-task-pages")
@pytest.mark.parametrize("target", ["plans/a", "phases/p1", "slices/a.first", "tasks/t1", "lane:eng", "kind:code"])
def test_get_takes_a_focus_target_and_its_task_list(target: str) -> None:
    assert ledger._ledger_item(f"focus/{target}") == f"focus/{target}"
    assert ledger._ledger_item(f"focus/{target}/tasks") == f"focus/{target}/tasks"


@pytest.mark.feature("ledger-focus-task-pages")
@pytest.mark.parametrize(
    "item",
    [
        "focus/",
        "focus/phases",
        "focus/phases/",
        "focus/chat/c1",
        "focus/lane:",
        "focus/lane:eng/",
        "focus/phases/p1/task",
        "focus/phases/p1/tasks/",
        "focus/phases/p1/tasks/t1",
        "focus/phases/p1?limit=1",
        "focus/phases/p1/tasks?limit=1",
        "focus/phases/..",
        "focus/tasks/a#b",
        "focus/tasks/t 1",
        "focusx/phases/p1",
        "orders/phases/p1/tasks",
    ],
)
def test_get_refuses_a_focus_item_that_names_no_target(item: str) -> None:
    with pytest.raises(SystemExit):
        ledger.build_parser().parse_args(["--slug", SLUG, "get", item])


@pytest.mark.feature("ledger-focus-task-pages")
def test_get_reads_a_focus_target_in_one_reply_and_its_task_list_to_the_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    reader = Served(kept(tmp_path, [task(f"t{number}") for number in range(150)]))

    def client(slug: str, timeout: float) -> Any:
        return reader

    monkeypatch.setattr(ledger, "client", client)
    monkeypatch.setattr(sys, "argv", ["ledger", "--slug", SLUG, "get", "focus/phases/p1"])
    ledger.main()
    one = json.loads(capsys.readouterr().out)
    assert one["target"] == "phases/p1" and len(one["tasks"]) == 150
    monkeypatch.setattr(sys, "argv", ["ledger", "--slug", SLUG, "get", LIST])
    ledger.main()
    rows = json.loads(capsys.readouterr().out)
    assert [plain(row) for row in rows] == one["tasks"]
    assert [urlsplit(path).path for path in reader.paths] == ["focus/phases/p1", LIST, LIST]
    assert [urlsplit(path).query.split("&")[0] for path in reader.paths] == ["", "limit=100", "limit=100"]


@pytest.mark.feature("ledger-focus-task-pages")
@pytest.mark.parametrize(
    ("path", "message"),
    [
        (
            "focus",
            f"The focus records pass the reply limit: list them with {READ} freezes "
            f"and read the tasks of one with {READ} focus/<target>/tasks",
        ),
        (
            "focus/phases/p1",
            f"The tasks under phases/p1 pass the reply limit: read them with {READ} focus/phases/p1/tasks",
        ),
        (
            "focus/lane:eng",
            f"The tasks under lane:eng pass the reply limit: read them with {READ} focus/lane:eng/tasks",
        ),
    ],
)
def test_a_focus_read_past_the_reply_limit_names_the_command_line_read_that_fits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str, message: str
) -> None:
    tasks = [task(f"t{number}") for number in range(40)]
    repo = kept(tmp_path, tasks, record("phases/p1", "focus"))
    whole = routes.ledger_read(served(repo), SLUG, path, {})
    size = resources.reply_size(whole)
    monkeypatch.setattr(resources, "MAX_REPLY", size)
    assert routes.ledger_read(served(repo), SLUG, path, {}) == whole
    monkeypatch.setattr(resources, "MAX_REPLY", size - 1)
    assert refusal(served(repo), path, {}) == (413, "resource_too_large", message)
    reader = Served(repo)
    assert [row["id"] for row in reader.collection(SLUG, LIST)] == [row["id"] for row in tasks]
    assert len(reader.paths) > 1


def through(reader: Served) -> Callable[[str, str, bool], Any]:
    """The read of the live check answered by the route code: a list through its pages, any other path in one
    reply."""

    def read(slug: str, path: str, listed: bool) -> Any:
        return reader.collection(slug, path) if listed else reader.request(slug, path)["data"]

    return read


@pytest.fixture
def crowded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Served, list[dict[str, Any]]]:
    """A ledger served by the route code whose focused phase holds more task rows than one reply, and those rows."""
    repo = kept(tmp_path, [task(f"t{number}") for number in range(250)], record("phases/p1", "focus"))
    whole = routes.ledger_read(served(repo), SLUG, "focus/phases/p1", {})["data"]
    monkeypatch.setattr(resources, "MAX_REPLY", resources.reply_size({"data": whole}) - 1)
    return Served(repo), whole["tasks"]


@pytest.mark.feature("ledger-focus-task-pages")
def test_the_live_check_holds_a_target_one_reply_cannot_hold_to_the_list_the_route_hashes(
    crowded: tuple[Served, list[dict[str, Any]]],
) -> None:
    reader, rows = crowded
    assert focus_task_pages.lines(SLUG, through(reader)) == [
        focus_task_pages.PAGED.format(slug=SLUG, target="phases/p1", count=250)
    ]
    assert [urlsplit(path).path for path in reader.paths] == ["focus", "freezes", "focus/phases/p1", *[LIST] * 4]
    assert parse_qs(urlsplit(reader.paths[-1]).query) == {"limit": ["1"], "cursor": [f"{resources.revision(rows)}:0"]}


@pytest.mark.feature("ledger-focus-task-pages")
def test_the_live_check_fails_a_target_whose_pages_miss_a_row_of_the_list_the_route_hashes(
    crowded: tuple[Served, list[dict[str, Any]]],
) -> None:
    reader, _ = crowded
    read = through(reader)

    def short(slug: str, path: str, listed: bool) -> Any:
        found = read(slug, path, listed)
        return found[:-1] if listed and path == LIST else found

    assert focus_task_pages.lines(SLUG, short) == [
        focus_task_pages.FAIL + focus_task_pages.BROKEN.format(slug=SLUG, target="phases/p1", paged=249)
    ]
    asked = [path for path in reader.paths if urlsplit(path).query.startswith("limit=1&")]
    assert len(asked) == focus_task_pages.ATTEMPTS
