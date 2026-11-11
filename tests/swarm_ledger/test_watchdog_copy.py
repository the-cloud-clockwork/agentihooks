"""The newest watchdog document copy is stored on the ledger and read back through the tick ledger port."""

import json

import pytest

from tests.swarm_ledger.test_api_v1 import authority_live as _authority_live
from tests.swarm_ledger.test_api_v1 import live as _live
from tests.swarm_ledger.test_ledger_authority import SLUG, ledger

authority_live = _authority_live
live = _live
pytestmark = pytest.mark.unit


@pytest.mark.feature("tick-doc-items-map")
def test_a_watchdog_copy_lands_on_the_ledger_and_the_tick_ledger_port_reads_the_newest(live):
    import hashlib
    from dataclasses import replace

    from scripts.swarm import doc_items
    from scripts.swarm_ledger.api.client import ResourceClient
    from scripts.tick.adapters.fakes import fake_tools
    from scripts.tick.adapters.ledger import ClientLedger
    from scripts.tick.deadline import Deadline

    credentials = ledger.credentials(SLUG, service=True)
    port = ClientLedger(SLUG, lambda timeout: ResourceClient(ledger.BASE, credentials, timeout))
    assert port.watchdog_copy(deadline=Deadline.after(5)) == {}
    client = ResourceClient(ledger.BASE, credentials)
    first, newest = "1. [open] First.\n", "1. [open] First.\n2. [open] Next.\n"
    for number, text in enumerate((first, newest, newest)):
        client.mutate(SLUG, [{"op": "watchdog_copy", "id": f"copy-{number}", "by": "swarm", "text": text}])
    copy = port.watchdog_copy(deadline=Deadline.after(5))
    assert {key: copy[key] for key in ("text", "digest", "by")} == {
        "text": newest,
        "digest": hashlib.sha256(newest.encode()).hexdigest(),
        "by": "swarm",
    }
    assert isinstance(copy["at"], int)
    state = client.request(SLUG, "export", {})["data"]
    stamp = state["_meta"]["stamps"]["watchdog_copy"]
    assert (stamp["by"], stamp["at"], isinstance(stamp["rev"], int)) == ("swarm", copy["at"], True)
    tools = replace(fake_tools(), ledger=port)
    assert doc_items.map_changed(state, tools, Deadline.after(5)) == [
        "mapped watchdog doc items of the ledger copy: 0 items on 0 tasks, updated none"
    ]
    events = [event for event in client.collection(SLUG, "events") if event.get("kind") == "watchdog copy stored"]
    assert [event["id"] for event in events] == ["copy-0", "copy-1"]


@pytest.mark.feature("tick-doc-items-map")
@pytest.mark.parametrize(
    "operation",
    [
        {"op": "watchdog_copy", "id": "c", "by": "swarm", "text": "   "},
        {"op": "watchdog_copy", "id": "c", "by": "swarm", "text": "x" * 100001},
        {"op": "watchdog_copy", "id": "c", "by": "1bad", "text": "copy"},
        {"op": "watchdog_copy", "id": "c", "by": "swarm", "text": "copy", "extra": 1},
    ],
)
def test_the_watchdog_copy_operation_refuses_an_empty_oversized_or_unsigned_copy(operation):
    from scripts.swarm_ledger import ledger_watchdog_copy

    with pytest.raises(ValueError):
        ledger_watchdog_copy.check(operation)
    assert ledger_watchdog_copy.check({"op": "watchdog_copy", "id": "c", "by": "swarm", "text": "x" * 100000}) is None


@pytest.mark.feature("tick-doc-items-map")
def test_the_doc_items_command_stores_the_watchdog_copy_from_a_file(tmp_path, monkeypatch, capsys):
    import hashlib

    from scripts.swarm import doc_items, ledger_client

    copy = tmp_path / "watchdog.md"
    copy.write_text("1. [open] First. [Ledger: t1]\n", encoding="utf-8")
    sent = []
    monkeypatch.setattr(ledger_client.LedgerClient, "_call", lambda self, slug, ops: sent.append((slug, ops)))
    assert doc_items.main(["--slug", SLUG, "--document", str(copy), "--store-as", "watchdog-1"]) == 0
    [(slug, [operation])] = sent
    assert slug == SLUG
    assert {key: operation[key] for key in ("op", "by", "text")} == {
        "op": "watchdog_copy",
        "by": "watchdog-1",
        "text": "1. [open] First. [Ledger: t1]\n",
    }
    digest = hashlib.sha256(b"1. [open] First. [Ledger: t1]\n").hexdigest()
    assert json.loads(capsys.readouterr().out) == {"slug": SLUG, "stored": digest}
