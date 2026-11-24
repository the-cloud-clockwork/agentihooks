"""Open rows pages stay inside the reply bound, the tasks open read folds in the frozen marks, and the summary read
keeps its crew window and one cached build per ledger."""

from types import SimpleNamespace
from typing import Any, cast

import pytest

from scripts.swarm_ledger import ledger_server
from scripts.swarm_ledger.api import resources, routes
from scripts.swarm_ledger.api import summary as summary_module
from scripts.swarm_ledger.api import task_list as task_list_module
from scripts.swarm_ledger.api.errors import APIError


def _row_at_reply_limit(row_id: str) -> dict[str, Any]:
    low, high, best = 0, resources.MAX_REPLY, 0
    while low <= high:
        mid = (low + high) // 2
        item = {"id": row_id, "pad": "x" * mid, "position": 0}
        size = resources.reply_size(resources.closed_reply([item], 1, 0))
        if size <= resources.MAX_REPLY:
            best = mid
            low = mid + 1
        else:
            high = mid - 1
    return {"id": row_id, "pad": "x" * best}


@pytest.mark.feature("ledger-summary-read")
def test_open_page_without_an_explicit_offset_or_limit_uses_the_defaults() -> None:
    rows = [(index, {"id": f"t{index}"}) for index in range(resources.OPEN_LIMIT + 1)]
    reply = resources.open_page(rows, {})
    assert len(reply["data"]) == resources.OPEN_LIMIT
    assert reply["next_offset"] == resources.OPEN_LIMIT


@pytest.mark.feature("ledger-summary-read")
def test_open_page_includes_a_single_row_whose_reply_lands_exactly_at_the_reply_limit() -> None:
    row = _row_at_reply_limit("solo")
    reply = resources.open_page([(0, row)], {})
    assert resources.reply_size(reply) == resources.MAX_REPLY
    assert reply["data"] == [{**row, "position": 0}]


@pytest.mark.feature("ledger-summary-read")
def test_open_page_stops_before_an_oversized_second_row_and_returns_the_rows_already_selected() -> None:
    small = {"id": "small"}
    huge = {"id": "huge", "pad": "x" * resources.MAX_REPLY}
    reply = resources.open_page([(0, small), (1, huge)], {})
    assert reply is not None
    assert [row["id"] for row in reply["data"]] == ["small"]
    assert reply["next_offset"] == 1


@pytest.mark.feature("ledger-summary-read")
def test_open_page_refuses_an_oversized_first_row_with_resource_too_large() -> None:
    huge_row = {"id": "huge", "pad": "x" * resources.MAX_REPLY}
    with pytest.raises(APIError) as exc_info:
        resources.open_page([(0, huge_row)], {})
    error = exc_info.value
    assert error.status == 413
    assert error.code == "resource_too_large"
    assert str(error) == "Use the explicit export operation for this resource"


@pytest.mark.feature("ledger-summary-read")
def test_open_read_tasks_path_reads_task_rows_and_folds_in_the_frozen_marks(monkeypatch: pytest.MonkeyPatch) -> None:
    rows: list[dict[str, Any]] = [{"id": "frozen-task"}, {"id": "loose-task"}]
    captured: dict[str, Any] = {}
    monkeypatch.setattr(routes.task_list, "rows", lambda repository, slug: (1, {}, rows))

    def fake_view_rows(path: str, given_rows: list[Any], frozen: set[str]) -> list[Any]:
        captured["args"] = (path, given_rows, frozen)
        return []

    monkeypatch.setattr(routes.summary, "view_rows", fake_view_rows)
    monkeypatch.setattr(routes.resources, "open_page", lambda view, query: view)
    services = cast(
        Any,
        SimpleNamespace(
            repository="repo-placeholder",
            ledger=SimpleNamespace(summary=lambda slug: {"marks": {"frozen": ["tasks/frozen-task"]}}),
        ),
    )
    result = routes.open_read(services, "slug-x", "tasks", {})
    assert captured["args"] == ("tasks", rows, {"tasks/frozen-task"})
    assert result == []


@pytest.mark.feature("ledger-summary-read")
def test_open_read_non_tasks_path_skips_marks_and_uses_an_empty_frozen_set(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(routes, "collection", lambda services, slug, path: ["row-a"])

    def fake_view_rows(path: str, given_rows: list[Any], frozen: set[str]) -> list[Any]:
        captured["args"] = (path, given_rows, frozen)
        return []

    monkeypatch.setattr(routes.summary, "view_rows", fake_view_rows)
    monkeypatch.setattr(routes.resources, "open_page", lambda view, query: view)
    services = cast(Any, SimpleNamespace())
    result = routes.open_read(services, "slug-x", "phases", {})
    assert captured["args"] == ("phases", ["row-a"], set())
    assert result == []


@pytest.mark.feature("ledger-summary-read")
def test_active_crew_keeps_members_with_future_last_seen_and_drops_those_missing_it_at_the_window_boundary() -> None:
    now = ledger_server.CREW_WINDOW_MS + 1
    meta = {
        "members": {
            "agent-no-seen": {"role": "member"},
            "agent-future": {"role": "member", "last_seen": now + 10_000},
        }
    }
    crew = ledger_server.active_crew(meta, now)
    assert [row["last_seen"] for row in crew] == [now + 10_000]


@pytest.mark.feature("ledger-summary-read")
def test_ledger_summary_reuses_the_cached_build_for_unchanged_stored_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    slug = "ledger-cache"
    stored_rows = (1, {"rev": 1}, [])
    monkeypatch.setattr(ledger_server, "SUMMARIES", {})
    monkeypatch.setattr(ledger_server, "SUMMARY_BUILDS", {})
    monkeypatch.setattr(task_list_module, "rows", lambda repository, given_slug: stored_rows)
    monkeypatch.setattr(ledger_server.repository, "read", lambda given_slug, *keys: {"_meta": {}})
    monkeypatch.setattr(ledger_server, "doctor_holds", lambda given_slug: ("", False))
    monkeypatch.setattr(ledger_server, "served_version", lambda: "page-token")
    builds: list[int] = []

    def fake_build(parts: dict[str, Any], tasks: Any, holds: Any, limits: Any) -> dict[str, Any]:
        builds.append(len(builds))
        return {"_meta": {}}

    monkeypatch.setattr(summary_module, "build", fake_build)
    first = ledger_server.ledger_summary(slug)
    second = ledger_server.ledger_summary(slug)
    assert first is second
    assert len(builds) == 1


@pytest.mark.feature("ledger-summary-read")
def test_ledger_summary_keeps_a_separate_build_lock_for_each_slug(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ledger_server, "SUMMARIES", {})
    monkeypatch.setattr(ledger_server, "SUMMARY_BUILDS", {})
    monkeypatch.setattr(task_list_module, "rows", lambda repository, given_slug: (1, {"rev": 1}, []))
    monkeypatch.setattr(ledger_server.repository, "read", lambda given_slug, *keys: {"_meta": {}})
    monkeypatch.setattr(ledger_server, "doctor_holds", lambda given_slug: ("", False))
    monkeypatch.setattr(ledger_server, "served_version", lambda: "page-token")
    monkeypatch.setattr(summary_module, "build", lambda *args: {"_meta": {}})
    ledger_server.ledger_summary("ledger-one")
    ledger_server.ledger_summary("ledger-two")
    assert set(ledger_server.SUMMARY_BUILDS) == {"ledger-one", "ledger-two"}


@pytest.mark.feature("ledger-summary-read")
def test_ledger_summary_builds_meta_page_version_crew_and_limits_from_doctor_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slug = "ledger-meta"
    stored_rows = (1, {"rev": 3, "members": {"agent-one": {"role": "member", "last_seen": 5}}}, [])
    monkeypatch.setattr(ledger_server, "SUMMARIES", {})
    monkeypatch.setattr(ledger_server, "SUMMARY_BUILDS", {})
    monkeypatch.setattr(ledger_server.core, "now_ms", lambda: 1_000_000)
    monkeypatch.setattr(task_list_module, "rows", lambda repository, given_slug: stored_rows)
    monkeypatch.setattr(ledger_server.repository, "read", lambda given_slug, *keys: {"_meta": stored_rows[1]})
    holds_calls: list[Any] = []

    def fake_doctor_holds(given_slug: str) -> tuple[str, bool]:
        holds_calls.append(given_slug)
        return "", False

    monkeypatch.setattr(ledger_server, "doctor_holds", fake_doctor_holds)
    monkeypatch.setattr(ledger_server, "served_version", lambda: "page-token")
    limits_seen: list[Any] = []

    def fake_build(parts: dict[str, Any], tasks: Any, holds: Any, limits: Any) -> dict[str, Any]:
        limits_seen.append(limits)
        return {"_meta": {}}

    monkeypatch.setattr(summary_module, "build", fake_build)
    built = ledger_server.ledger_summary(slug)
    assert built["_meta"]["page_version"] == "page-token"
    assert [row["last_seen"] for row in built["_meta"]["crew"]] == [5]
    assert limits_seen == [ledger_server.PAGE_LIMITS]
    assert holds_calls == [slug]
