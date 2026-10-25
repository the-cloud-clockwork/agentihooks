import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest
from nats.js.errors import NotFoundError

from scripts.events import nats_names
from scripts.events.resource_names import consumer_name


@pytest.mark.parametrize(
    ("results", "expected"),
    [([object()], "old"), ([NotFoundError(), object()], "new"), ([NotFoundError(), NotFoundError()], "new")],
)
def test_stream_selection_preserves_existing_resources(results, expected):
    js = SimpleNamespace(stream_info=AsyncMock(side_effect=results))
    assert asyncio.run(nats_names.existing_stream(js, "new", "old")) == expected
    assert js.stream_info.await_args_list == [call("old"), *([call("new")] if len(results) == 2 else [])]


def test_stream_selection_propagates_lookup_errors():
    js = SimpleNamespace(stream_info=AsyncMock(side_effect=PermissionError("denied")))
    with pytest.raises(PermissionError, match="denied"):
        asyncio.run(nats_names.existing_stream(js, "new", "old"))
    js.stream_info.assert_awaited_once_with("old")


def test_clean_consumers_need_no_compatibility_lookup():
    js = SimpleNamespace(consumer_info=AsyncMock())
    assert asyncio.run(nats_names.existing_consumer(js, "stream", "reader", "reader")) == "reader"
    js.consumer_info.assert_not_awaited()


@pytest.mark.parametrize(
    ("results", "legacy"),
    [([object()], True), ([NotFoundError(), object()], False), ([NotFoundError(), NotFoundError()], False)],
)
def test_consumer_selection_keeps_the_stored_cursor(results, legacy):
    js = SimpleNamespace(consumer_info=AsyncMock(side_effect=results))
    desired = consumer_name("reader_old")
    assert asyncio.run(nats_names.existing_consumer(js, "stream", consumer_name("reader_old"), "reader_old")) == (
        "reader_old" if legacy else desired
    )
    assert js.consumer_info.await_args_list == [
        call("stream", "reader_old"),
        *([call("stream", desired)] if len(results) == 2 else []),
    ]


def test_consumer_selection_propagates_lookup_errors():
    js = SimpleNamespace(consumer_info=AsyncMock(side_effect=PermissionError("denied")))
    with pytest.raises(PermissionError, match="denied"):
        asyncio.run(nats_names.existing_consumer(js, "stream", consumer_name("reader_old"), "reader_old"))
    js.consumer_info.assert_awaited_once_with("stream", "reader_old")


def test_dotted_logical_consumers_never_query_an_invalid_legacy_name():
    js = SimpleNamespace(consumer_info=AsyncMock())
    desired = consumer_name("hub.task_ready")
    assert asyncio.run(nats_names.existing_consumer(js, "stream", desired, "hub.task_ready")) == desired
    js.consumer_info.assert_not_awaited()


@pytest.mark.parametrize(
    ("permissions", "expected", "queried"),
    [
        ({"allow": ["$JS.API.STREAM.INFO.>"]}, "new", True),
        ({"allow": []}, "new", True),
        ({"allow": ["$JS.API.CONSUMER.INFO.>"]}, "old", False),
        ({"deny": ["$JS.API.STREAM.INFO.old"]}, "old", False),
    ],
)
def test_root_selection_respects_credential_discovery_permissions(monkeypatch, permissions, expected, queried):
    js = SimpleNamespace(stream_info=AsyncMock(side_effect=NotFoundError()))
    from unittest.mock import Mock

    parse = Mock(return_value=("jwt", "key"))
    claims = Mock(return_value={"nats": {"pub": permissions}})
    monkeypatch.setattr(nats_names.nkey, "parse_creds", parse)
    monkeypatch.setattr(nats_names.nkey, "claims", claims)
    assert asyncio.run(nats_names.root_stream(js, "new", "old", "creds")) == expected
    parse.assert_called_once_with("creds")
    claims.assert_called_once_with("jwt")
    assert js.stream_info.await_count == (2 if queried else 0)


def test_root_selection_without_credentials_discovers_existing_streams():
    js = SimpleNamespace(stream_info=AsyncMock())
    assert asyncio.run(nats_names.root_stream(js, "new", "old", None)) == "old"
    js.stream_info.assert_awaited_once_with("old")
