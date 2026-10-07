import urllib.error
from itertools import count

import pytest

from scripts.gates import Who
from scripts.swarm import trace_plan
from scripts.swarm.ledger_client import LedgerClient
from scripts.swarm_ledger.api.client import ResourceClient
from tests.swarm.test_trace_plan import DOGHOUSE, Recorder, answers
from tests.swarm_ledger.test_api_v1 import authority_live as _authority_live
from tests.swarm_ledger.test_api_v1 import live as _live
from tests.swarm_ledger.test_ledger_authority import SLUG, core, ledger

pytestmark = pytest.mark.xdist_group("fakeredis")

authority_live = _authority_live
live = _live


def busy(monkeypatch):
    request, writes = ResourceClient.request, count()

    def write_after(self, slug, path, payload=None):
        reply = request(self, slug, path, payload)
        core.sync(SLUG, ops=[{"op": "add", "id": f"busy-{next(writes)}", "thread": "chat", "text": "Busy"}])
        return reply

    monkeypatch.setattr(ResourceClient, "request", write_after)
    return writes


def test_state_reads_one_record_while_a_write_lands_after_every_request(live, monkeypatch):
    writes = busy(monkeypatch)
    state = LedgerClient(service=True).state(SLUG)
    assert next(writes) == 1
    assert state["phases"][0]["title"] == "Proof"
    assert [row["text"] for row in state["chat"]] == ["First message", "Second message"]
    assert "api-reader" in state["_meta"]["members"]
    assert state["_meta"]["crew"]
    assert state["_meta"]["events"]
    assert "seeds" not in state["_meta"]


def test_trace_plan_files_its_cut_on_a_ledger_that_changed_since_the_read(live, monkeypatch, tmp_path):
    core.sync(
        SLUG, ops=[{"op": "task_add", "id": "add-t1", "by": "swarm", "task": "t1", "title": "Doghouse", "lane": "eng"}]
    )
    busy(monkeypatch)
    monkeypatch.setattr(trace_plan, "decide", Recorder(answers(0.9, 0.8, 0.1)))
    folder = tmp_path / "t1"
    folder.mkdir()
    (folder / trace_plan.PLAN).write_text(DOGHOUSE)
    client = LedgerClient()
    state = trace_plan.intent(client.state(SLUG), "t1")
    core.sync(SLUG, ops=[{"op": "add_item", "id": "other-cut", "by": "swarm", "list": "followups", "text": "Other"}])
    who = Who(name="engineer@1-1", swarm=SLUG, task="t1")
    record, block = trace_plan.run(folder, state, client, who, "observe", home=tmp_path / "home")
    assert (record["verdict"], block) == ("pass", False)
    texts = [row["text"] for row in client._resource(SLUG, "followups", collection=True)]
    assert texts == ["Other", "Cut from the plan of task t1: a diesel generator"]


def test_a_write_against_a_stale_collection_revision_is_still_refused(live):
    client = ResourceClient(ledger.BASE, ledger.credentials(SLUG, service=True))
    stale = client.request(SLUG, "followups")["revision"]
    client.mutate(SLUG, [{"op": "add_item", "id": "first", "by": "swarm", "list": "followups", "text": "First"}])
    with pytest.raises(urllib.error.HTTPError) as refused:
        client.mutate(
            SLUG,
            [
                {
                    "op": "add_item",
                    "id": "late",
                    "by": "swarm",
                    "list": "followups",
                    "text": "Late",
                    "expected_revision": stale,
                }
            ],
        )
    assert refused.value.code == 409
    assert [row["text"] for row in client.collection(SLUG, "followups")] == ["First"]
