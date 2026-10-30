import io
import json
import urllib.error
from types import SimpleNamespace

import pytest

from scripts.swarm import ledger_client


def served(monkeypatch, answer):
    reads = []

    def resource(slug, path, service, collection):
        reads.append((slug, path, service, collection))
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
    assert reads == [("demo", "tasks/t2", True, False)]


def test_a_task_id_cannot_name_a_thread_or_another_resource(monkeypatch):
    reads = served(monkeypatch, refused(404))
    assert ledger_client.LedgerClient().task("demo", "t1/comments") is None
    assert reads == [("demo", "tasks/t1%2Fcomments", False, False)]


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
    assert reads == [("demo", "tasks?targets=https%3A%2F%2Fgithub.com%2Fo%2Fr%2Fpull%2F7", False, False)]


def test_a_ledger_without_targeting_falls_back_to_filtering_its_tasks(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "https://github.com/o/r/pull/7"
    rows = [{"id": "t1", "pr_url": url}, {"id": "t2", "pr_url": url + "0"}, {"id": "t3", "split_pulls": [url]}]
    reads = []

    def resource(slug: str, path: str, service: bool, collection: bool) -> list[dict]:
        reads.append((path, collection))
        if not collection:
            raise refused(400, "schema_invalid")
        return rows

    monkeypatch.setattr(ledger_client, "_ledger", lambda: SimpleNamespace(resource=resource))
    assert [row["id"] for row in ledger_client.LedgerClient().tasks_targeting("demo", url)] == ["t1", "t3"]
    assert [collection for _, collection in reads] == [False, True]


@pytest.mark.parametrize("code,error", [(400, "revision_conflict"), (500, "schema_invalid")])
def test_any_other_refusal_of_a_targeting_read_is_raised(
    monkeypatch: pytest.MonkeyPatch, code: int, error: str
) -> None:
    served(monkeypatch, refused(code, error))
    with pytest.raises(urllib.error.HTTPError) as raised:
        ledger_client.LedgerClient().tasks_targeting("demo", "https://github.com/o/r/pull/7")
    assert raised.value.code == code
