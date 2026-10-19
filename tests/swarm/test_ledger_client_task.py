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
