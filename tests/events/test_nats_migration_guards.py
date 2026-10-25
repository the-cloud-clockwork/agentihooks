import asyncio
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from nats.errors import TimeoutError as FetchTimeout
from nats.js.api import StreamConfig
from nats.js.errors import APIError, NotFoundError

from scripts.events import nats_migration as migration
from scripts.events.bus import StateEntry
from scripts.events.config import BusConfig, QueuePolicy
from scripts.events.envelope import Envelope
from scripts.events.nats import ATTEMPT_BASE, MSG_ID
from scripts.events.nats_state import IMPORT_PENDING, LAST_SUBJECT_SEQUENCE, REVISION, REVISION_BASE

pytestmark = pytest.mark.unit


def backend(metadata=None, messages=0):
    info = SimpleNamespace(
        config=StreamConfig(name="target", metadata=metadata), state=SimpleNamespace(messages=messages)
    )
    js = SimpleNamespace(stream_info=AsyncMock(return_value=info), update_stream=AsyncMock(), publish=AsyncMock())
    return SimpleNamespace(
        _config=BusConfig(scoped=True),
        _loop=SimpleNamespace(run=asyncio.run),
        _js=js,
        _legacy_dead=SimpleNamespace(_stream="AGENTIHOOKS_DEAD"),
    )


def message(swarm="rig", sequence=7):
    event = Envelope("id", "ready", swarm, "task", "work", 1, 0, "author", {})
    return SimpleNamespace(
        data=event.encode(),
        headers=None,
        ack_sync=AsyncMock(),
        metadata=SimpleNamespace(sequence=SimpleNamespace(stream=sequence), num_delivered=3),
    )


@pytest.mark.parametrize(
    "config,error,text",
    [
        (BusConfig(scoped=True, provision=False), PermissionError, "migration requires privileged provisioning"),
        (BusConfig(), ValueError, "scoped migration is not enabled"),
    ],
)
def test_migration_requires_a_privileged_scoped_root(config, error, text):
    with pytest.raises(error) as caught:
        migration.require_migration(SimpleNamespace(_config=config))
    assert str(caught.value) == text


def test_state_import_begin_preserves_revision_base_and_sets_pending_barrier():
    root = backend({migration.IMPORT_PLAN: "plan", REVISION_BASE: "25", IMPORT_PENDING: "state"})
    target = SimpleNamespace(_stream="target")
    expected = {migration.IMPORT_PLAN: "plan", REVISION_BASE: "25", IMPORT_PENDING: "state"}
    assert migration._begin_state(root, target, "plan", 99) == expected
    assert target._revision_base == 25
    assert root._js.update_stream.await_args.kwargs["config"].metadata == expected
    root._js.stream_info.return_value.config.metadata = {migration.IMPORT_PLAN: "plan", REVISION_BASE: "25"}
    root._js.update_stream.reset_mock()
    assert migration._begin_state(root, target, "plan", 99) is None
    root._js.update_stream.assert_not_awaited()


@pytest.mark.parametrize(
    "metadata,messages,text",
    [
        ({migration.IMPORT_PLAN: "other"}, 0, "state migration ownership or source revisions changed"),
        ({}, 1, "scoped state already contains live writes"),
    ],
)
def test_state_import_refuses_a_different_plan_or_existing_live_data(metadata, messages, text):
    with pytest.raises(ValueError) as caught:
        migration._begin_state(backend(metadata, messages), SimpleNamespace(_stream="target"), "plan", 99)
    assert str(caught.value) == text


def test_resuming_state_copy_skips_only_imported_records_and_pins_revision_tokens():
    root = backend()
    first, second = StateEntry("a", b"first", 4), StateEntry("a", b"next", 9)
    target = SimpleNamespace(
        _stream="target",
        watch=lambda _: [first],
        _last=AsyncMock(return_value=SimpleNamespace(seq=2)),
        _subject=lambda key: "swarm.rig.state.work." + key,
    )
    migration._copy_state(root, target, [first, second], "plan")
    root._js.publish.assert_awaited_once_with(
        "swarm.rig.state.work.a",
        b"next",
        stream="target",
        headers={REVISION: "9", LAST_SUBJECT_SEQUENCE: "2", MSG_ID: "state-import:plan:9"},
    )


@pytest.mark.parametrize("mode", ["new", "existing", "race"])
def test_work_copy_acknowledges_only_a_matching_durable_copy(mode):
    root = backend()
    work = message()
    target = SimpleNamespace(_stream="target", _filter="swarm.rig.queue.eng.>")
    retained = SimpleNamespace(data=work.data)
    root._js.get_last_msg = AsyncMock(
        side_effect=[NotFoundError(), retained] if mode == "race" else None, return_value=retained
    )
    if mode == "new":
        root._js.get_last_msg.side_effect = NotFoundError()
    if mode == "race":
        root._js.publish.side_effect = APIError(err_code=10071)
    asyncio.run(migration._copy_work(root, target, work))
    root._js.get_last_msg.assert_awaited_with("target", "swarm.rig.queue.eng.migration.7")
    if mode != "existing":
        root._js.publish.assert_awaited_once_with(
            "swarm.rig.queue.eng.migration.7",
            work.data,
            stream="target",
            headers={MSG_ID: "migration:target:7", LAST_SUBJECT_SEQUENCE: "0", ATTEMPT_BASE: "2"},
        )
    work.ack_sync.assert_awaited_once_with()


@pytest.mark.parametrize("race", [False, True])
def test_work_copy_never_acknowledges_a_conflicting_retained_copy(race):
    root = backend()
    work = message()
    conflict = SimpleNamespace(data=b"other")
    root._js.get_last_msg = AsyncMock(return_value=conflict)
    error = APIError(err_code=10071)
    if race:
        root._js.get_last_msg.side_effect = [NotFoundError(), conflict]
        root._js.publish.side_effect = error
    with pytest.raises(APIError if race else ValueError) as caught:
        asyncio.run(
            migration._copy_work(root, SimpleNamespace(_stream="target", _filter="swarm.rig.queue.eng.>"), work)
        )
    if race:
        assert caught.value is error
    else:
        assert str(caught.value) == "retained migration copy differs from its source"
    work.ack_sync.assert_not_awaited()


@pytest.mark.parametrize("case", ["timeout", "bare timeout", "foreign", "newer", "valid"])
def test_queue_drain_has_a_finite_budget_and_refuses_source_changes(monkeypatch, case):
    root = backend()
    root._js.stream_info.side_effect = [
        SimpleNamespace(state=SimpleNamespace(last_seq=7, messages=1)),
        SimpleNamespace(state=SimpleNamespace(last_seq=7, messages=0)),
    ]
    work = message("other" if case == "foreign" else "rig", 8 if case == "newer" else 7)
    timeouts = {"timeout": FetchTimeout(), "bare timeout": asyncio.TimeoutError()}
    fetch = AsyncMock(side_effect=timeouts.get(case), return_value=[work])
    source = SimpleNamespace(_stream="source", _consumer=SimpleNamespace(_subscription=SimpleNamespace(fetch=fetch)))
    copied = AsyncMock()
    monkeypatch.setattr(migration, "_copy_work", copied)
    policy = QueuePolicy(ack_wait_ms=1234, backoff_ms=(10, 20))
    if case == "valid":
        asyncio.run(migration._drain_work(root, source, "target", policy, "rig"))
        copied.assert_awaited_once_with(root, "target", work)
    else:
        with pytest.raises(RuntimeError if case in timeouts else ValueError) as caught:
            asyncio.run(migration._drain_work(root, source, "target", policy, "rig"))
        assert str(caught.value) == (
            "legacy queue still has undelivered work"
            if case in timeouts
            else "legacy queue changed or contains work for another swarm"
        )
        copied.assert_not_awaited()
    fetch.assert_awaited_once_with(100, timeout=2.264)


@pytest.mark.parametrize("race", [False, True])
def test_dead_import_walks_holes_and_includes_last_record_with_conditional_writes(race):
    root = backend()
    own, other = message(), message("other")
    root._js.stream_info.return_value = SimpleNamespace(state=SimpleNamespace(first_seq=1, last_seq=3))
    calls = []

    async def get_msg(stream, *, seq):
        assert (stream, seq) == (root._legacy_dead._stream, len(calls) + 1)
        calls.append(seq)
        if seq == 1:
            raise NotFoundError()
        return other if seq == 2 else own

    root._js.get_msg = get_msg
    destination = "swarm.rig.dead." + hashlib.sha256(b"id").hexdigest()
    root._js.get_last_msg = AsyncMock(return_value=SimpleNamespace(data=own.data))
    if race:
        root._js.publish.side_effect = APIError(err_code=10071)
    asyncio.run(migration._copy_dead(root, "rig", "target"))
    assert calls == [1, 2, 3]
    root._js.publish.assert_awaited_once_with(
        destination, own.data, stream="target", headers={MSG_ID: "id", LAST_SUBJECT_SEQUENCE: "0"}
    )
    if race:
        root._js.get_last_msg.assert_awaited_once_with("target", destination)


@pytest.mark.parametrize("changed", [False, True])
def test_state_handover_pins_the_ownership_plan_and_checks_source_revisions(monkeypatch, changed):
    import json
    from unittest.mock import Mock

    root = backend()
    records = [StateEntry("a", b"last", 9), StateEntry("a", b"first", 4)]
    source = SimpleNamespace(
        watch=lambda key: records if key == "a" else [],
        get=lambda key: SimpleNamespace(revision=10 if changed else 9) if key == "a" else None,
    )
    root.state = Mock(return_value=source)
    root._js.stream_info.return_value.state.last_seq = 17
    target = SimpleNamespace(_stream="target")
    construct = Mock(return_value=target)
    begin = Mock(return_value={IMPORT_PENDING: "state", "preserved": "yes"})
    copy = Mock()
    metadata = AsyncMock()
    monkeypatch.setattr(migration, "ScopedStateStore", construct)
    monkeypatch.setattr(migration, "_begin_state", begin)
    monkeypatch.setattr(migration, "_copy_state", copy)
    monkeypatch.setattr(migration, "_metadata", metadata)
    if changed:
        with pytest.raises(RuntimeError) as caught:
            migration.migrate_state(root, "rig", "work", ["a", "missing", "a"])
        assert str(caught.value) == "legacy state changed during migration"
        metadata.assert_not_awaited()
    else:
        migration.migrate_state(root, "rig", "work", ["a", "missing", "a"])
        metadata.assert_awaited_once_with(root, "target", {"preserved": "yes"})
    plan = hashlib.sha256(
        json.dumps({"keys": ["a", "missing"], "revisions": [("a", 4), ("a", 9)]}).encode()
    ).hexdigest()
    construct.assert_called_once_with(root, "rig", "work", importing=True)
    begin.assert_called_once_with(root, target, plan, 17)
    copy.assert_called_once_with(root, target, list(reversed(records)), plan)


def test_first_state_import_sets_the_source_highwater_before_copying():
    root = backend({})
    target = SimpleNamespace(_stream="target")
    result = migration._begin_state(root, target, "plan", 99)
    assert result == {migration.IMPORT_PLAN: "plan", REVISION_BASE: "99", IMPORT_PENDING: "state"}
    assert target._revision_base == 99
    assert root._js.update_stream.await_args.kwargs["config"].metadata == result


def test_queue_handover_holds_the_import_barrier_until_drain_is_confirmed(monkeypatch):
    from unittest.mock import Mock

    root = backend({"preserved": "yes"})
    source = SimpleNamespace(_stream="source")
    target = SimpleNamespace(_stream="target")
    constructor = Mock(return_value=source)
    root.for_swarm = Mock(return_value=SimpleNamespace(queue=Mock(return_value=target)))
    snapshots = []

    async def metadata(current, stream, changes):
        assert current is root and stream == "target"
        snapshots.append(dict(changes))

    async def drain(current, original, destination, policy, swarm):
        assert snapshots == [{"preserved": "yes", IMPORT_PENDING: "queue"}]
        assert (current, original, destination, swarm) == (root, source, target, "rig")

    monkeypatch.setattr(migration, "NatsWorkQueue", constructor)
    monkeypatch.setattr(migration, "_metadata", metadata)
    monkeypatch.setattr(migration, "_drain_work", drain)
    policy = QueuePolicy(max_deliver=2)
    migration.migrate_queue(root, "rig", "eng", policy)
    assert snapshots == [{"preserved": "yes", IMPORT_PENDING: "queue"}, {"preserved": "yes"}]
    args = constructor.call_args.args
    assert args[:4] == (root._loop, root._js, "swarm.rig.ready.eng", policy)
    assert args[4].scoped is False
    root.for_swarm.assert_called_once_with("rig")
    root.for_swarm.return_value.queue.assert_called_once_with("eng", policy)


def test_dead_handover_uses_the_selected_swarm_stream(monkeypatch):
    from unittest.mock import Mock

    root = backend()
    root.for_swarm = Mock(return_value=SimpleNamespace(dead=SimpleNamespace(_log=SimpleNamespace(_stream="target"))))
    copy = AsyncMock()
    monkeypatch.setattr(migration, "_copy_dead", copy)
    migration.migrate_dead(root, "rig")
    copy.assert_awaited_once_with(root, "rig", "target")
