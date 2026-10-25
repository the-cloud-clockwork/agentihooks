import importlib
import json

import pytest

from scripts.events.config import CREDENTIALS_ENV, URL_ENV, QueuePolicy
from scripts.events.envelope import Envelope
from tests.events.test_nats_migration import buses as buses
from tests.hive.test_nats_grant import signer as signer

pytestmark = pytest.mark.unit


@pytest.fixture
def command(buses, monkeypatch):
    import nats

    _, scoped = buses
    monkeypatch.setenv("AGENTIHOOKS_BUS", "nats")
    monkeypatch.setenv(URL_ENV, scoped._config.url)
    monkeypatch.setenv(CREDENTIALS_ENV, scoped._config.credentials)
    monkeypatch.delenv("AGENTIHOOKS_BUS_BIND", raising=False)
    original = nats.connect

    async def connect(url, *args, **kwargs):
        assert url == scoped._config.url
        return await original(url, *args, **kwargs)

    monkeypatch.setattr(nats, "connect", connect)
    return importlib.import_module("scripts.events.migrate_ready")


def test_command_inventories_then_keeps_every_ready_offer(buses, command, capsys):
    legacy, scoped = buses
    policy = QueuePolicy()
    offers = {}
    for swarm, lane, count in [("doctor", "ci", 7), ("doctor", "eng", 3), ("rig", "plan", 2)]:
        queue = legacy.queue(f"swarm.{swarm}.ready.{lane}", policy)
        offers[swarm, lane] = [
            Envelope(f"{swarm}-{lane}-{index}", "task.ready", swarm, "task", str(index), 1, 0, "hub", {"lane": lane})
            for index in range(count)
        ]
        for offer in offers[swarm, lane]:
            queue.offer(offer)

    assert command.main([]) == 0
    inventory = json.loads(capsys.readouterr().out)
    assert inventory["retained"] == 12
    assert inventory["kept"] == 0
    assert inventory["applied"] is False
    assert command.main(["--apply", "--quiescent", "--swarm", "doctor", "--swarm", "rig"]) == 0
    moved = json.loads(capsys.readouterr().out)
    assert moved["retained"] == moved["kept"] == 12
    assert moved["remaining"] == 0
    assert {(row["swarm"], row["lane"], row["kept"]) for row in moved["queues"]} == {
        ("doctor", "ci", 7),
        ("doctor", "eng", 3),
        ("rig", "plan", 2),
    }
    assert command.main(["--apply", "--quiescent", "--swarm", "doctor", "--swarm", "rig"]) == 0
    repeated = json.loads(capsys.readouterr().out)
    assert repeated["retained"] == repeated["kept"] == repeated["remaining"] == 0
    for (swarm, lane), expected in offers.items():
        target = scoped.for_swarm(swarm).queue(lane, policy)
        received = []
        for _ in expected:
            delivery = target.take(1000)
            assert delivery is not None
            received.append(delivery.event)
            delivery.ack()
        assert received == expected
        assert target.take(20) is None


def test_command_reports_observed_counts_when_migration_fails_after_copy(buses, command, monkeypatch, capsys):
    legacy, scoped = buses
    offer = Envelope("retained", "task.ready", "doctor", "task", "work", 1, 0, "hub", {"lane": "eng"})
    legacy.queue("swarm.doctor.ready.eng", QueuePolicy()).offer(offer)
    migrate = command.NatsBus.migrate_queue

    def interrupted(bus, swarm, name, policy):
        migrate(bus, swarm, name, policy)
        raise RuntimeError("interrupted after copy")

    monkeypatch.setattr(command.NatsBus, "migrate_queue", interrupted)
    assert command.main(["--apply", "--quiescent", "--swarm", "doctor"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["retained"] == report["kept"] == 1
    assert report["remaining"] == 0
    assert report["queues"][0]["kept"] == 1
    assert report["queues"][0]["remaining"] == 0
    assert report["failures"] == [{"swarm": "doctor", "lane": "eng", "error": "interrupted after copy"}]
    assert scoped.for_swarm("doctor").queue("eng", QueuePolicy()).take(1000).event == offer


@pytest.mark.parametrize("arguments", [["--apply"], ["--apply", "--quiescent"], ["--apply", "--swarm", "doctor"]])
def test_apply_requires_named_quiet_scope(command, arguments, capsys):
    with pytest.raises(SystemExit) as error:
        command.main(arguments)
    assert error.value.code == 2
    assert (
        capsys.readouterr()
        .err.splitlines()[-1]
        .endswith(": error: --apply requires explicit --swarm selection and --quiescent")
    )


@pytest.mark.parametrize(("backend", "bind"), [("memory", "0"), ("nats", "1")])
def test_apply_requires_privileged_nats(command, monkeypatch, capsys, backend, bind):
    monkeypatch.setenv("AGENTIHOOKS_BUS", backend)
    monkeypatch.setenv("AGENTIHOOKS_BUS_BIND", bind)
    with pytest.raises(SystemExit) as error:
        command.main(["--apply", "--quiescent", "--swarm", "doctor"])
    assert error.value.code == 2
    assert (
        capsys.readouterr()
        .err.splitlines()[-1]
        .endswith(": error: migration requires the NATS backend and privileged provisioning for --apply")
    )


def test_help_explains_selection_and_quiet_window(command, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["migrate-ready"])
    with pytest.raises(SystemExit) as error:
        command.main(["--help"])
    assert error.value.code == 0
    assert " ".join(capsys.readouterr().out.split()) == (
        "usage: migrate-ready [-h] [--swarm SWARM] [--apply] [--quiescent] "
        "Inventory or migrate retained legacy ready offers to scoped queues options: "
        "-h, --help show this help message and exit "
        "--swarm SWARM Limit the move to this swarm; repeat for more "
        "--apply Move retained offers; default is read only inventory "
        "--quiescent Confirm legacy producers and consumers are quiet"
    )


def test_inventory_is_read_only_and_move_keeps_foreign_scope(buses, command, monkeypatch, capsys):
    from nats.js.client import JetStreamContext

    legacy, scoped = buses
    owned = Envelope("owned", "task.ready", "doctor", "task", "one", 1, 0, "hub", {})
    foreign = Envelope("foreign", "task.ready", "other", "task", "two", 1, 0, "hub", {})
    legacy.queue("swarm.doctor.ready.eng", QueuePolicy()).offer(owned)
    outside = legacy.queue("swarm.other.ready.ci", QueuePolicy())
    outside.offer(foreign)
    before = [info.config.name for info in legacy._loop.run(legacy._js.streams_info())]

    async def refuse_write(*args, **kwargs):
        raise AssertionError("inventory attempted stream provisioning")

    with monkeypatch.context() as read_only:
        read_only.setattr(JetStreamContext, "add_stream", refuse_write)
        assert command.main([]) == 0
    inventory = json.loads(capsys.readouterr().out)
    assert inventory["retained"] == inventory["remaining"] == 2
    assert inventory["failures"] == []
    assert [info.config.name for info in legacy._loop.run(legacy._js.streams_info())] == before
    assert command.main(["--apply", "--quiescent", "--swarm", "doctor"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["applied"] is True
    assert report["retained"] == report["kept"] == 1
    assert report["remaining"] == 0
    assert report["failures"] == []
    assert [(row["swarm"], row["lane"]) for row in report["queues"]] == [("doctor", "eng")]
    assert scoped.for_swarm("doctor").queue("eng", QueuePolicy()).take(1000).event == owned
    assert outside.take(1000).event == foreign


def test_inventory_reads_every_page(buses, command, monkeypatch, capsys):
    from nats.js.client import JetStreamContext

    legacy, _ = buses
    names = set()
    for lane in ("eng", "ci", "plan"):
        queue = legacy.queue(f"swarm.doctor.ready.{lane}", QueuePolicy())
        queue.offer(Envelope(lane, "task.ready", "doctor", "task", lane, 1, 0, "hub", {}))
        names.add(queue._stream)
    original = JetStreamContext.streams_info_iterator
    requested = []

    async def one_per_page(client, *, offset):
        assert offset == len(requested)
        requested.append(offset)
        page = await original(client, offset=0)
        streams = [stream for stream in page.streams if stream["config"]["name"] in names]
        page.total = len(streams)
        page.streams = streams[offset : offset + 1]
        return page

    monkeypatch.setattr(JetStreamContext, "streams_info_iterator", one_per_page)
    assert command.main([]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["retained"] == 3
    assert [(row["swarm"], row["lane"]) for row in report["queues"]] == [
        ("doctor", "ci"),
        ("doctor", "eng"),
        ("doctor", "plan"),
    ]


def test_incomplete_inventory_is_refused(command, monkeypatch):
    from nats.js.client import JetStreamContext

    original = JetStreamContext.streams_info_iterator
    requested = []

    async def empty_page(client, *, offset):
        assert not requested
        requested.append(offset)
        page = await original(client, offset=offset)
        page.total = 1
        page.streams = []
        return page

    monkeypatch.setattr(JetStreamContext, "streams_info_iterator", empty_page)
    with pytest.raises(RuntimeError) as error:
        command.main([])
    assert str(error.value) == "NATS stream inventory did not advance"


def test_inventory_refuses_conflicting_ready_ownership(buses, command):
    legacy, _ = buses
    legacy._loop.run(legacy._js.add_stream(name="foreign", subjects=["agentihooks.queue.swarm.doctor.ready.eng"]))
    with pytest.raises(ValueError) as error:
        command.main([])
    assert str(error.value) == "legacy ready queue ownership differs from its stream name"


@pytest.mark.parametrize("change", ["pending", "new_offer", "new_consumed", "unreadable"])
def test_incomplete_move_reports_failure_and_unknown_counts(buses, command, monkeypatch, capsys, change):
    from nats.js.client import JetStreamContext
    from nats.js.errors import APIError

    legacy, _ = buses
    source = legacy.queue("swarm.doctor.ready.eng", QueuePolicy())
    source.offer(Envelope("one", "task.ready", "doctor", "task", "one", 1, 0, "hub", {}))
    migrate = command.NatsBus.migrate_queue
    stream_info = JetStreamContext.stream_info

    async def unavailable(client, name, **kwargs):
        if name == source._stream:
            raise APIError(description="source probe unavailable")
        return await stream_info(client, name, **kwargs)

    def incomplete(bus, swarm, name, policy):
        if change == "pending":
            return
        migrate(bus, swarm, name, policy)
        if change in ("new_offer", "new_consumed"):
            source.offer(Envelope("two", "task.ready", "doctor", "task", "two", 1, 0, "hub", {}))
            if change == "new_consumed":
                delivery = source.take(1000)
                source._loop.run(delivery._msg.ack_sync())
        else:
            monkeypatch.setattr(JetStreamContext, "stream_info", unavailable)
            raise RuntimeError("original move interrupted")

    monkeypatch.setattr(command.NatsBus, "migrate_queue", incomplete)
    assert command.main(["--apply", "--quiescent", "--swarm", "doctor"]) == 1
    report = json.loads(capsys.readouterr().out)
    expected = {
        "pending": (0, 1, "legacy ready queue changed during migration"),
        "new_offer": (None, 1, "legacy ready queue changed during migration"),
        "new_consumed": (None, 0, "legacy ready queue changed during migration"),
        "unreadable": (None, None, "original move interrupted"),
    }
    kept, remaining, message = expected[change]
    assert report["kept"] == kept
    assert report["remaining"] == remaining
    assert report["failures"] == [{"swarm": "doctor", "lane": "eng", "error": message}]


def test_move_refuses_work_that_changed_after_inventory(buses, command, monkeypatch, capsys):
    from nats.js.client import JetStreamContext

    legacy, _ = buses
    source = legacy.queue("swarm.doctor.ready.eng", QueuePolicy())
    source.offer(Envelope("one", "task.ready", "doctor", "task", "one", 1, 0, "hub", {}))
    original = JetStreamContext.stream_info
    changed = False

    async def changing(client, name, **kwargs):
        nonlocal changed
        if name == source._stream and not changed:
            changed = True
            source.offer(Envelope("two", "task.ready", "doctor", "task", "two", 1, 0, "hub", {}))
        return await original(client, name, **kwargs)

    monkeypatch.setattr(JetStreamContext, "stream_info", changing)
    assert command.main(["--apply", "--quiescent", "--swarm", "doctor"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["retained"] == 1
    assert report["kept"] == 0
    assert report["remaining"] == 2
    assert report["failures"] == [
        {"swarm": "doctor", "lane": "eng", "error": "legacy ready queue changed after inventory"}
    ]
