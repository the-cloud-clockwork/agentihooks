import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from nats.js.errors import APIError, NotFoundError

from scripts.events.bus import StateConflict, StateEntry
from scripts.events.nats_state import IMPORT_PENDING, LAST_SUBJECT_SEQUENCE, REVISION, REVISION_BASE, ScopedStateStore
from scripts.events.subjects import stream_name

pytestmark = pytest.mark.unit


def root(metadata=None, provision=False):
    info = SimpleNamespace(config=SimpleNamespace(metadata=metadata), state=SimpleNamespace(messages=0))
    js = SimpleNamespace(
        stream_info=AsyncMock(return_value=info),
        add_stream=AsyncMock(),
        publish=AsyncMock(return_value=SimpleNamespace(seq=8)),
        get_last_msg=AsyncMock(side_effect=NotFoundError()),
        get_msg=AsyncMock(side_effect=NotFoundError()),
    )
    return SimpleNamespace(_config=SimpleNamespace(provision=provision), _loop=SimpleNamespace(run=asyncio.run), _js=js)


def test_state_reads_imported_revisions_and_preserves_empty_values():
    backend = root({REVISION_BASE: "100"})
    state = ScopedStateStore(backend, "rig", "work")
    backend._js.get_last_msg.side_effect = None
    backend._js.get_last_msg.return_value = SimpleNamespace(seq=3, data=None, headers={REVISION: "42"})
    assert state.get("a.b") == StateEntry(key="a.b", value=b"", revision=42)
    backend._js.get_last_msg.assert_awaited_once_with(stream_name("rig", "STATE_work"), "swarm.rig.state.work.a.b")
    backend._js.get_last_msg.return_value = SimpleNamespace(seq=3, data=b"value", headers=None)
    assert state.get("a.b") == StateEntry(key="a.b", value=b"value", revision=103)


def test_state_conditional_write_keeps_the_physical_sequence_and_revision_base():
    backend = root({REVISION_BASE: "100"})
    state = ScopedStateStore(backend, "rig", "work")
    assert state.create("a.b", b"first") == 108
    backend._js.publish.assert_awaited_once_with(
        "swarm.rig.state.work.a.b",
        b"first",
        stream=stream_name("rig", "STATE_work"),
        headers={LAST_SUBJECT_SEQUENCE: "0"},
    )
    backend._js.get_last_msg.side_effect = None
    backend._js.get_last_msg.return_value = SimpleNamespace(seq=7, headers={REVISION: "42"})
    assert state.update("a.b", b"next", 42) == 108
    assert backend._js.publish.await_args.kwargs["headers"] == {LAST_SUBJECT_SEQUENCE: "7"}


@pytest.mark.parametrize("operation", ["create", "update"])
@pytest.mark.parametrize("code", [10071, 10099])
def test_state_maps_only_conditional_write_conflicts(operation, code):
    backend = root()
    state = ScopedStateStore(backend, "rig", "work")
    backend._js.get_last_msg.side_effect = None
    backend._js.get_last_msg.return_value = SimpleNamespace(seq=7, headers=None)
    error = APIError(err_code=code)
    backend._js.publish.side_effect = error
    args = ("key", b"value") if operation == "create" else ("key", b"value", 7)
    with pytest.raises(StateConflict if code == 10071 else APIError) as caught:
        getattr(state, operation)(*args)
    if code == 10071:
        expected = "state key 'key' exists" if operation == "create" else "state key 'key' is not at revision 7"
        assert str(caught.value) == expected
    else:
        assert caught.value is error


def test_state_refuses_missing_and_stale_revisions_without_writing():
    backend = root()
    state = ScopedStateStore(backend, "rig", "work")
    for message in [None, SimpleNamespace(seq=8, headers=None)]:
        backend._js.get_last_msg.side_effect = None
        backend._js.get_last_msg.return_value = message
        with pytest.raises(StateConflict) as caught:
            state.update("key", b"bad", 7)
        assert str(caught.value) == "state key 'key' is not at revision 7"
    backend._js.publish.assert_not_awaited()


def test_state_watch_advances_sparse_sequences_and_filters_imported_revisions():
    backend = root({REVISION_BASE: "100"})
    state = ScopedStateStore(backend, "rig", "work")
    calls = []

    async def get_msg(stream, *, seq, subject, next):
        expected = [1, 4, 10][len(calls)]
        assert (stream, seq, subject, next) == (
            stream_name("rig", "STATE_work"),
            expected,
            "swarm.rig.state.work.a.b",
            True,
        )
        calls.append(seq)
        if len(calls) == 3:
            raise NotFoundError()
        return SimpleNamespace(seq=[3, 9][len(calls) - 1], data=None, headers={REVISION: str([41, 43][len(calls) - 1])})

    backend._js.get_msg = get_msg
    assert list(state.watch("a.b", 42)) == [StateEntry(key="a.b", value=b"", revision=43)]
    assert calls == [1, 4, 10]


def test_state_watch_translates_new_revision_floor_to_physical_sequence():
    backend = root({REVISION_BASE: "100"})
    state = ScopedStateStore(backend, "rig", "work")
    assert list(state.watch("key", 107)) == []
    backend._js.get_msg.assert_awaited_once_with(
        stream_name("rig", "STATE_work"), seq=7, subject="swarm.rig.state.work.key", next=True
    )


def test_members_cannot_bind_an_unfinished_state_import():
    with pytest.raises(RuntimeError) as caught:
        ScopedStateStore(root({IMPORT_PENDING: "state"}), "rig", "work")
    assert str(caught.value) == "scoped state migration is incomplete"


@pytest.mark.parametrize("importing", [False, True])
def test_new_state_stream_records_history_and_import_barrier(importing):
    backend = root(provision=True)
    ready = SimpleNamespace(config=SimpleNamespace(metadata={}), state=SimpleNamespace(messages=0))
    backend._js.stream_info.side_effect = [NotFoundError(), NotFoundError(), ready]
    ScopedStateStore(backend, "rig", "work", importing=importing)
    assert backend._js.stream_info.await_args_list[1].args == ("KV_work",)
    backend._js.add_stream.assert_awaited_once_with(
        name=stream_name("rig", "STATE_work"),
        subjects=["swarm.rig.state.work.>"],
        max_msgs_per_subject=64,
        metadata={IMPORT_PENDING: "true"} if importing else None,
    )


def test_retained_legacy_state_requires_authoritative_key_ownership():
    backend = root(provision=True)
    backend._js.stream_info.side_effect = [NotFoundError(), SimpleNamespace(state=SimpleNamespace(messages=1))]
    with pytest.raises(RuntimeError) as caught:
        ScopedStateStore(backend, "rig", "work")
    assert str(caught.value) == "legacy state requires explicit key ownership"
    backend._js.add_stream.assert_not_awaited()
