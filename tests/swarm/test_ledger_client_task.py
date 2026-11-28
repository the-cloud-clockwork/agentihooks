import io
import json
import urllib.error
from types import SimpleNamespace

import pytest

from scripts.swarm import ledger_client


def served(monkeypatch, answer):
    reads = []

    def resource(slug, path, collection=False):
        reads.append((slug, path, collection))
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(ledger_client, "_ledger", lambda: SimpleNamespace(resource=resource))
    return reads


def refused(code, error="resource_missing"):
    body = json.dumps({"error": {"code": error, "message": "refused"}}).encode()
    return urllib.error.HTTPError("http://ledger", code, "refused", {}, io.BytesIO(body))


def test_a_task_read_names_one_task_and_returns_its_row(monkeypatch):
    reads = served(monkeypatch, {"id": "t2", "state": "open"})
    assert ledger_client.LedgerClient(service=True).task("demo", "t2") == {"id": "t2", "state": "open"}
    assert reads == [("demo", "tasks/t2", False)]


def test_a_task_id_cannot_name_a_thread_or_another_resource(monkeypatch):
    reads = served(monkeypatch, refused(404))
    assert ledger_client.LedgerClient().task("demo", "t1/comments") is None
    assert reads == [("demo", "tasks/t1%2Fcomments", False)]


def test_a_task_the_ledger_does_not_hold_reads_as_none(monkeypatch):
    served(monkeypatch, refused(404))
    assert ledger_client.LedgerClient().task("demo", "t9") is None


@pytest.mark.parametrize("code,error", [(404, "ledger_missing"), (403, "forbidden"), (500, "resource_missing")])
def test_any_other_refusal_of_a_task_read_is_raised_with_its_body(monkeypatch, code, error):
    served(monkeypatch, refused(code, error))
    with pytest.raises(urllib.error.HTTPError) as raised:
        ledger_client.LedgerClient().task("demo", "t2")
    assert raised.value.code == code
    assert json.loads(raised.value.read())["error"]["code"] == error


def test_a_targeting_read_asks_the_ledger_for_the_tasks_naming_one_pull(monkeypatch: pytest.MonkeyPatch) -> None:
    own, other = {"id": "t2", "pr_url": "https://github.com/o/r/pull/7"}, {"id": "t3"}
    reads = served(monkeypatch, [own, other])
    assert ledger_client.LedgerClient().tasks_targeting("demo", "https://github.com/o/r/pull/7") == [own]
    assert reads == [("demo", "tasks?targets=https%3A%2F%2Fgithub.com%2Fo%2Fr%2Fpull%2F7", False)]


@pytest.mark.feature("master-freeze-queue")
def test_a_freeze_read_asks_the_ledger_for_its_whole_freeze_collection(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [{"verb": "freeze", "target": "tasks/t2", "by": "master@a1b2c3-0001"}]
    reads = served(monkeypatch, rows)
    assert ledger_client.LedgerClient().freezes("demo") == rows
    assert reads == [("demo", "freezes", True)]


@pytest.mark.feature("swarm-enhance-sweep")
def test_a_work_tree_read_asks_the_ledger_for_its_plans_phases_and_slices_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [{"id": "p1", "plan": "plans/a"}]
    reads = served(monkeypatch, rows)
    assert ledger_client.LedgerClient().work_tree("demo") == {"plans": rows, "phases": rows, "slices": rows}
    assert reads == [("demo", "plans", True), ("demo", "phases", True), ("demo", "slices", True)]


def test_an_alerts_read_asks_the_ledger_for_its_alert_collection_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [{"id": "a1", "source": "seat", "state": "open", "text": "no master"}]
    reads = served(monkeypatch, rows)
    assert ledger_client.LedgerClient().alerts("demo") == rows
    assert reads == [("demo", "alerts", True)]


def test_a_ledger_without_targeting_falls_back_to_filtering_its_tasks(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "https://github.com/o/r/pull/7"
    rows = [{"id": "t1", "pr_url": url}, {"id": "t2", "pr_url": url + "0"}, {"id": "t3", "split_pulls": [url]}]
    reads = []

    def resource(slug: str, path: str, collection: bool = False) -> list[dict]:
        reads.append((slug, path, collection))
        if not collection:
            raise refused(400, "schema_invalid")
        return rows

    monkeypatch.setattr(ledger_client, "_ledger", lambda: SimpleNamespace(resource=resource))
    assert [row["id"] for row in ledger_client.LedgerClient().tasks_targeting("demo", url)] == ["t1", "t3"]
    assert [(slug, collection) for slug, _, collection in reads] == [("demo", False), ("demo", True)]
    assert reads[1][1] == "tasks"


@pytest.mark.parametrize("code,error", [(400, "revision_conflict"), (500, "schema_invalid")])
def test_any_other_refusal_of_a_targeting_read_is_raised(
    monkeypatch: pytest.MonkeyPatch, code: int, error: str
) -> None:
    url = "https://github.com/o/r/pull/7"

    def resource(slug: str, path: str, collection: bool = False) -> list[dict]:
        if not collection:
            raise refused(code, error)
        return [{"id": "t1", "pr_url": url}]

    monkeypatch.setattr(ledger_client, "_ledger", lambda: SimpleNamespace(resource=resource))
    with pytest.raises(urllib.error.HTTPError) as raised:
        ledger_client.LedgerClient().tasks_targeting("demo", url)
    assert raised.value.code == code


@pytest.mark.feature("plan-lane-admission-narrow-read")
def test_a_phase_read_asks_the_ledger_for_the_tasks_of_those_phases_cut_to_their_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads = served(monkeypatch, [{"id": "t1", "title": "T"}])

    found = ledger_client.LedgerClient().tasks_in_phases("demo", iter(["p1", "p2"]), ("title", "state"))

    assert found == [{"id": "t1", "title": "T"}]
    assert reads == [("demo", "tasks?fields=title%2Cstate&phases=p1%2Cp2", False)]


PHASED_ROWS = [
    {"id": "t1", "phase": "p1", "title": "A", "comments": [{"text": "x"}]},
    {"id": "t2", "phase": "p3", "title": "B"},
    {"id": "t3", "phase": "p2", "title": "C", "state": "done"},
]


def phase_refusing(code: int, error: str, reads: list[tuple[str, str, bool]]) -> object:
    def resource(slug: str, path: str, collection: bool = False) -> list[dict[str, object]]:
        reads.append((slug, path, collection))
        if not collection:
            raise refused(code, error)
        return PHASED_ROWS

    return SimpleNamespace(resource=resource)


@pytest.mark.feature("plan-lane-admission-narrow-read")
def test_a_ledger_without_the_phase_selection_falls_back_to_selecting_its_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads: list[tuple[str, str, bool]] = []
    monkeypatch.setattr(ledger_client, "_ledger", lambda: phase_refusing(400, "schema_invalid", reads))

    found = ledger_client.LedgerClient().tasks_in_phases("demo", iter(["p1", "p2"]), ("title", "state"))

    assert found == [{"id": "t1", "title": "A"}, {"id": "t3", "state": "done", "title": "C"}]
    assert reads[1:] == [("demo", "tasks", True)]


@pytest.mark.feature("plan-lane-admission-narrow-read")
@pytest.mark.parametrize("code,error", [(400, "revision_conflict"), (500, "schema_invalid")])
def test_any_other_refusal_of_a_phase_read_is_raised(monkeypatch: pytest.MonkeyPatch, code: int, error: str) -> None:
    reads: list[tuple[str, str, bool]] = []
    monkeypatch.setattr(ledger_client, "_ledger", lambda: phase_refusing(code, error, reads))

    with pytest.raises(urllib.error.HTTPError) as raised:
        ledger_client.LedgerClient().tasks_in_phases("demo", ["p1"])

    assert raised.value.code == code
    assert [collection for _, _, collection in reads] == [False]


@pytest.mark.feature("plan-lane-admission-narrow-read")
def test_a_thread_read_pages_one_record_thread_by_its_quoted_id(monkeypatch: pytest.MonkeyPatch) -> None:
    entries = [{"id": "c1", "text": "landed"}]
    reads = served(monkeypatch, entries)

    assert ledger_client.LedgerClient().thread("demo", "phases/p 1", "comments") == entries
    assert reads == [("demo", "phases/p%201/comments", True)]


@pytest.mark.feature("plan-lane-admission-narrow-read")
def test_an_artifacts_read_pages_the_artifact_collection_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [{"id": "artifact_add-a1", "plan": True, "file": {"id": "plan-file"}}]
    reads = served(monkeypatch, rows)

    assert ledger_client.LedgerClient().artifacts("demo") == rows
    assert reads == [("demo", "artifacts", True)]


def test_a_watchdog_copy_read_names_the_watchdog_resource(monkeypatch):
    reads = served(monkeypatch, {"text": "copy", "digest": "d"})
    assert ledger_client.LedgerClient(service=True).watchdog_copy("demo") == {"text": "copy", "digest": "d"}
    assert reads == [("demo", "watchdog", False)]
