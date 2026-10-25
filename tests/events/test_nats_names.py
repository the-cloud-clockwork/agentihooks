import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest
from nats.js.errors import NotFoundError

from scripts.events import nats_names
from scripts.events.resource_names import consumer_name


@pytest.mark.parametrize(
    ("results", "expected"),
    [([object()], "new"), ([NotFoundError(), object()], "old"), ([NotFoundError(), NotFoundError()], "new")],
)
def test_stream_selection_preserves_existing_resources(results, expected):
    js = SimpleNamespace(stream_info=AsyncMock(side_effect=results))
    assert asyncio.run(nats_names.existing_stream(js, "new", "old")) == expected
    assert js.stream_info.await_args_list == [call("new"), *([call("old")] if len(results) == 2 else [])]


def test_stream_selection_propagates_lookup_errors():
    js = SimpleNamespace(stream_info=AsyncMock(side_effect=PermissionError("denied")))
    with pytest.raises(PermissionError, match="denied"):
        asyncio.run(nats_names.existing_stream(js, "new", "old"))
    js.stream_info.assert_awaited_once_with("new")


def test_clean_consumers_need_no_compatibility_lookup():
    js = SimpleNamespace(consumer_info=AsyncMock())
    assert asyncio.run(nats_names.existing_consumer(js, "stream", "reader")) == "reader"
    js.consumer_info.assert_not_awaited()


@pytest.mark.parametrize(
    ("results", "legacy"),
    [([object()], False), ([NotFoundError(), object()], True), ([NotFoundError(), NotFoundError()], False)],
)
def test_consumer_selection_keeps_the_stored_cursor(results, legacy):
    js = SimpleNamespace(consumer_info=AsyncMock(side_effect=results))
    desired = consumer_name("reader_old")
    assert asyncio.run(nats_names.existing_consumer(js, "stream", "reader_old")) == (
        "reader_old" if legacy else desired
    )
    assert js.consumer_info.await_args_list == [
        call("stream", desired),
        *([call("stream", "reader_old")] if len(results) == 2 else []),
    ]


def test_consumer_selection_propagates_lookup_errors():
    js = SimpleNamespace(consumer_info=AsyncMock(side_effect=PermissionError("denied")))
    with pytest.raises(PermissionError, match="denied"):
        asyncio.run(nats_names.existing_consumer(js, "stream", "reader_old"))
    js.consumer_info.assert_awaited_once_with("stream", consumer_name("reader_old"))
