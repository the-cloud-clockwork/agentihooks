"""The newest watchdog document copy is stored on the ledger and read back through the tick ledger port."""

import json

import pytest

from scripts.swarm_ledger import ledger as ledger_cli
from tests.swarm_ledger.test_api_v1 import authority_live as _authority_live
from tests.swarm_ledger.test_api_v1 import live as _live
from tests.swarm_ledger.test_ledger_authority import SLUG, ledger

authority_live = _authority_live
live = _live
pytestmark = pytest.mark.unit


def test_a_watchdog_copy_lands_on_the_ledger_and_the_tick_ledger_port_reads_the_newest(live):
    import hashlib

    from scripts.swarm_ledger.api.client import ResourceClient
    from scripts.tick.adapters.ledger import ClientLedger
    from scripts.tick.deadline import Deadline

    credentials = ledger.credentials(SLUG, service=True)
    port = ClientLedger(SLUG, lambda timeout: ResourceClient(ledger.BASE, credentials, timeout))
    assert port.watchdog_copy(deadline=Deadline.after(5)) == {}
    client = ResourceClient(ledger.BASE, credentials)
    first, newest = "1. [open] First. [Ledger: t1]\n", "1. [open] First. [Ledger: t1]\n2. [open] Next. [Ledger: t1]\n"
    for number, text in enumerate((first, newest, newest)):
        client.mutate(SLUG, [{"op": "watchdog_copy", "id": f"copy-{number}", "by": "swarm", "text": text}])
    copy = port.watchdog_copy(deadline=Deadline.after(5))
    assert {key: copy[key] for key in ("text", "digest", "by")} == {
        "text": newest,
        "digest": hashlib.sha256(newest.encode()).hexdigest(),
        "by": "swarm",
    }
    assert isinstance(copy["at"], int)
    stamp = client.request(SLUG, "export", {})["data"]["_meta"]["stamps"]["watchdog_copy"]
    assert (stamp["by"], stamp["at"]) == ("swarm", copy["at"])
    events = [event for event in client.collection(SLUG, "events") if event.get("kind") == "watchdog copy stored"]
    assert [event["id"] for event in events] == ["copy-0", "copy-1"]


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


def test_the_ledger_command_stores_the_watchdog_copy_from_a_file(tmp_path, monkeypatch, capsys):
    import hashlib
    from argparse import Namespace

    copy = tmp_path / "watchdog.md"
    copy.write_text("1. [open] First. [Ledger: t1]\n", encoding="utf-8")
    sent = []

    def send(args, kind, **fields):
        sent.append((kind, fields))
        return {"applied": [1]}

    monkeypatch.setattr(ledger_cli, "send", send)
    ledger_cli.cmd_watchdog_copy(Namespace(slug=SLUG, name="watchdog-1", path=str(copy)))
    assert sent == [("watchdog_copy", {"text": "1. [open] First. [Ledger: t1]\n"})]
    digest = hashlib.sha256(b"1. [open] First. [Ledger: t1]\n").hexdigest()
    assert json.loads(capsys.readouterr().out) == {"stored": True, "digest": digest}
    parsed = ledger_cli.build_parser().parse_args(["--slug", SLUG, "--as", "w", "watchdog-copy", str(copy)])
    assert (parsed.command, parsed.path) == ("watchdog-copy", str(copy))
