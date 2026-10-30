"""Tests for hooks.context.file_read_cache."""

import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


class TestFileReadCache:
    """Tests for file read deduplication cache."""

    def setup_method(self):
        """Clear in-memory cache before each test."""
        from hooks.context import file_read_cache

        file_read_cache._memory_cache.clear()

    def test_mark_and_was_file_read(self, tmp_path):
        """mark_file_read then was_file_read returns True."""
        from hooks.context.file_read_cache import mark_file_read, was_file_read

        f = tmp_path / "test.py"
        f.write_text("content")
        session_id = "session-abc"

        with patch("hooks.context.file_read_cache.get_redis", return_value=None):
            mark_file_read(session_id, str(f))
            assert was_file_read(session_id, str(f)) is True

    def test_unread_file_returns_false(self, tmp_path):
        """was_file_read returns False for never-read file."""
        from hooks.context.file_read_cache import was_file_read

        f = tmp_path / "unread.py"
        f.write_text("content")

        with patch("hooks.context.file_read_cache.get_redis", return_value=None):
            assert was_file_read("session-xyz", str(f)) is False

    def test_mtime_change_allows_reread(self, tmp_path):
        """After marking, modifying file mtime → was_file_read returns False."""
        from hooks.context.file_read_cache import mark_file_read, was_file_read

        f = tmp_path / "modified.py"
        f.write_text("original content")
        session_id = "session-mtime"

        with patch("hooks.context.file_read_cache.get_redis", return_value=None):
            mark_file_read(session_id, str(f))
            assert was_file_read(session_id, str(f)) is True

            # Modify mtime by 2 seconds into the future
            old_mtime = os.stat(str(f)).st_mtime
            os.utime(str(f), (old_mtime + 2, old_mtime + 2))

            assert was_file_read(session_id, str(f)) is False

    def test_clear_session_cache(self, tmp_path):
        """mark 3 files, clear, was_file_read → False for all."""
        from hooks.context.file_read_cache import (
            clear_session_cache,
            mark_file_read,
            was_file_read,
        )

        session_id = "session-clear"
        files = []
        for i in range(3):
            f = tmp_path / f"file{i}.py"
            f.write_text(f"content {i}")
            files.append(str(f))

        with patch("hooks.context.file_read_cache.get_redis", return_value=None):
            for fp in files:
                mark_file_read(session_id, fp)
            for fp in files:
                assert was_file_read(session_id, fp) is True

            clear_session_cache(session_id)

            for fp in files:
                assert was_file_read(session_id, fp) is False

    def test_cross_session_isolation(self, tmp_path):
        """Session A reads file; session B's was_file_read → False."""
        from hooks.context.file_read_cache import mark_file_read, was_file_read

        f = tmp_path / "shared.py"
        f.write_text("content")

        with patch("hooks.context.file_read_cache.get_redis", return_value=None):
            mark_file_read("session-A", str(f))
            assert was_file_read("session-A", str(f)) is True
            assert was_file_read("session-B", str(f)) is False

    def test_memory_fallback(self, tmp_path):
        """Redis unavailable → file_read_cache still works via memory dict."""
        from hooks.context.file_read_cache import mark_file_read, was_file_read

        f = tmp_path / "fallback.py"
        f.write_text("content")
        session_id = "session-fallback"

        with patch("hooks.context.file_read_cache.get_redis", return_value=None):
            assert was_file_read(session_id, str(f)) is False
            mark_file_read(session_id, str(f))
            assert was_file_read(session_id, str(f)) is True

    def test_check_and_block_raises_for_read_file(self, tmp_path):
        """check_and_block_redundant_read raises BlockAction for already-read file."""
        from hooks.context.file_read_cache import check_and_block_redundant_read, mark_file_read
        from hooks.hook_manager import BlockAction

        f = tmp_path / "blocked.py"
        f.write_text("content")
        session_id = "session-block"
        payload = {"session_id": session_id, "tool_input": {"file_path": str(f)}}

        with patch("hooks.context.file_read_cache.get_redis", return_value=None):
            mark_file_read(session_id, str(f))
            with pytest.raises(BlockAction):
                check_and_block_redundant_read(payload)

    def test_check_and_block_allows_first_read(self, tmp_path):
        """check_and_block_redundant_read does not raise on first read."""
        from hooks.context.file_read_cache import check_and_block_redundant_read

        f = tmp_path / "first_read.py"
        f.write_text("content")
        session_id = "session-first"
        payload = {"session_id": session_id, "tool_input": {"file_path": str(f)}}

        with patch("hooks.context.file_read_cache.get_redis", return_value=None):
            # Should not raise
            check_and_block_redundant_read(payload)


@pytest.fixture(params=[False, True], ids=["memory", "redis"])
def partial_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> dict[str, Any]:
    from hooks import config, hook_manager
    from hooks.context import file_read_cache

    file_read_cache._memory_cache.clear()
    import fakeredis

    redis = fakeredis.FakeRedis() if request.param else None
    monkeypatch.setattr(file_read_cache, "get_redis", lambda: redis)
    monkeypatch.setattr(config, "FILE_READ_CACHE_ENABLED", True)
    monkeypatch.setattr(config, "SECRETS_MODE", "off")
    for flag in (
        "QUOTA_POLICY_ENABLED",
        "QUOTA_USAGE_INJECTION_ENABLED",
        "BRAIN_ENABLED",
        "BROADCAST_ENABLED",
        "ENFORCEMENT_INJECTION_ENABLED",
        "RETRY_BREAKER_ENABLED",
        "CONTEXT_AUDIT_ENABLED",
    ):
        monkeypatch.setattr(config, flag, False)
    monkeypatch.setattr(hook_manager, "_operator_question", lambda _: None)
    monkeypatch.setattr(hook_manager, "_operator_reminder", lambda _: None)
    monkeypatch.setattr(hook_manager, "_swarm_after_tool", lambda _: None)
    monkeypatch.setattr(hook_manager, "_swarm_heartbeat", lambda *args, **kwargs: None)
    monkeypatch.setattr(hook_manager, "_credential_guard", lambda *args: None)
    monkeypatch.setattr(hook_manager, "_plan_read_guard", lambda *args: None)
    monkeypatch.setattr("hooks.context.swarm_gates.pretool", lambda *args: None)
    monkeypatch.setattr("hooks.lifecycle.guard.pretool", lambda _: None)
    monkeypatch.setattr("hooks.context.conditions.pre_effect", lambda _: None)
    monkeypatch.setattr("hooks.context.conditions.post_effect", lambda _: None)
    monkeypatch.setattr("hooks.context.conditions.write_guard", lambda *args: None)
    monkeypatch.setattr("hooks.targets.capabilities.can_inject_context", lambda _: True)
    monkeypatch.setattr("hooks.tool_memory.inject_memory", lambda **kwargs: None)
    monkeypatch.setattr("hooks.tool_memory.record_error", lambda _: None)
    path = tmp_path / "opening.md"
    path.write_text("opening line\n" * 1545)
    return {
        "session_id": "partial-opening",
        "tool_name": "Read",
        "tool_input": {"file_path": str(path)},
        "tool_response": {
            "type": "text",
            "file": {"startLine": 1, "numLines": 402, "totalLines": 1545, "content": "opening line\n" * 402},
        },
    }


@pytest.mark.parametrize(
    "extra",
    [
        {"offset": 403},
        {"offset": 403, "limit": 400},
        {"offset": 403, "allow_large": True},
        {"offset": 400, "limit": 10},
        {},
    ],
)
def test_partial_read_allows_unread_lines(partial_read: dict[str, Any], extra: dict[str, Any]) -> None:
    from hooks.hook_manager import on_post_tool_use, on_pre_tool_use

    on_post_tool_use(partial_read)
    on_pre_tool_use({**partial_read, "tool_input": {**partial_read["tool_input"], **extra}})


@pytest.mark.parametrize("extra", [{"offset": 1, "limit": 402}, {"offset": 200, "limit": 100}])
def test_partial_read_still_refuses_delivered_lines(partial_read: dict[str, Any], extra: dict[str, Any]) -> None:
    from hooks.hook_manager import BlockAction, on_post_tool_use, on_pre_tool_use

    on_post_tool_use(partial_read)
    with pytest.raises(BlockAction, match="already in context"):
        on_pre_tool_use({**partial_read, "tool_input": {**partial_read["tool_input"], **extra}})


@pytest.mark.parametrize(
    "failure",
    [
        {"is_error": True},
        {"tool_response": {"is_error": True}},
        {"tool_response": "Error reading file"},
        {"tool_response": {}},
    ],
)
def test_failed_or_unknown_read_does_not_mark_file(partial_read: dict[str, Any], failure: dict[str, Any]) -> None:
    from hooks.hook_manager import on_post_tool_use, on_pre_tool_use

    on_post_tool_use({**partial_read, **failure})
    on_pre_tool_use({**partial_read, "tool_input": {**partial_read["tool_input"], "offset": 1, "limit": 10}})


def test_read_ranges_accumulate_without_filling_gaps(partial_read: dict[str, Any]) -> None:
    from hooks.hook_manager import BlockAction, on_post_tool_use, on_pre_tool_use

    on_post_tool_use(partial_read)
    tail = {**partial_read, "tool_response": {"file": {"startLine": 500, "numLines": 1046, "totalLines": 1545}}}
    on_post_tool_use(tail)
    with pytest.raises(BlockAction):
        on_pre_tool_use({**tail, "tool_input": {**tail["tool_input"], "offset": 500}})
    on_pre_tool_use({**partial_read, "tool_input": {**partial_read["tool_input"], "offset": 400, "limit": 110}})
    middle = {**partial_read, "tool_response": {"file": {"startLine": 403, "numLines": 97, "totalLines": 1545}}}
    on_post_tool_use(middle)
    with pytest.raises(BlockAction):
        on_pre_tool_use(partial_read)


def test_changed_file_drops_previous_read_ranges(partial_read: dict[str, Any]) -> None:
    from hooks.hook_manager import on_post_tool_use, on_pre_tool_use

    on_post_tool_use(partial_read)
    path = Path(partial_read["tool_input"]["file_path"])
    mtime = path.stat().st_mtime
    os.utime(path, (mtime + 2, mtime + 2))
    on_post_tool_use(
        {**partial_read, "tool_response": {"file": {"startLine": 403, "numLines": 100, "totalLines": 1545}}}
    )
    on_pre_tool_use({**partial_read, "tool_input": {**partial_read["tool_input"], "offset": 1, "limit": 402}})
    on_pre_tool_use({**partial_read, "session_id": "other-session"})


def test_numbered_read_output_tracks_only_returned_lines(partial_read: dict[str, Any]) -> None:
    from hooks.hook_manager import BlockAction, on_post_tool_use, on_pre_tool_use

    on_post_tool_use({**partial_read, "tool_response": "     1→first\n     2→second\nRead more with offset 3"})
    on_pre_tool_use({**partial_read, "tool_input": {**partial_read["tool_input"], "offset": 3}})
    with pytest.raises(BlockAction):
        on_pre_tool_use({**partial_read, "tool_input": {**partial_read["tool_input"], "limit": 2}})


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        ({}, []),
        ({"startLine": 0, "numLines": 1}, []),
        ({"startLine": 1, "numLines": 0}, []),
        ({"startLine": "1", "numLines": 1}, []),
        ({"startLine": 1, "numLines": "1"}, []),
        ({"startLine": 1, "numLines": 1}, [(1, 1)]),
    ],
)
def test_delivered_metadata_requires_positive_line_counts(
    metadata: dict[str, Any], expected: list[tuple[int, int]]
) -> None:
    from hooks.context.file_read_cache import _delivered_ranges

    assert _delivered_ranges({"file": metadata}) == expected
    assert _delivered_ranges({}) == []


def test_one_unread_line_and_missing_first_line_stay_readable(partial_read: dict[str, Any]) -> None:
    from hooks.context.file_read_cache import mark_read_result, was_file_read
    from hooks.hook_manager import on_post_tool_use, on_pre_tool_use

    on_post_tool_use(partial_read)
    on_pre_tool_use({**partial_read, "tool_input": {**partial_read["tool_input"], "offset": 403, "limit": 1}})
    tail = {**partial_read, "tool_response": {"file": {"startLine": 404, "numLines": 1142, "totalLines": 1545}}}
    on_post_tool_use(tail)
    on_pre_tool_use({**partial_read, "tool_input": {**partial_read["tool_input"], "offset": 403, "limit": 1}})
    session_id = "missing-first-line"
    mark_read_result(
        {
            **tail,
            "session_id": session_id,
            "tool_response": {"file": {"startLine": 2, "numLines": 1544, "totalLines": 1545}},
        }
    )
    assert not was_file_read(session_id, partial_read["tool_input"]["file_path"])


@pytest.mark.parametrize("error_location", ["payload", "response"])
def test_failed_read_with_line_metadata_never_records_coverage(
    partial_read: dict[str, Any], error_location: str
) -> None:
    from hooks.context.file_read_cache import mark_read_result, was_file_read

    payload = dict(partial_read)
    if error_location == "payload":
        payload["is_error"] = True
    else:
        payload["tool_response"] = {**payload["tool_response"], "is_error": True}
    mark_read_result(payload)
    assert not was_file_read(payload["session_id"], payload["tool_input"]["file_path"], 1, 1)


def test_tool_output_records_numbered_lines_without_tool_response(partial_read: dict[str, Any]) -> None:
    from hooks.context.file_read_cache import mark_read_result, was_file_read

    payload = {key: value for key, value in partial_read.items() if key != "tool_response"}
    payload["tool_output"] = "1→first line\n2→second line"
    mark_read_result(payload)
    assert was_file_read(payload["session_id"], payload["tool_input"]["file_path"], 1, 2)
    assert not was_file_read(payload["session_id"], payload["tool_input"]["file_path"], 1, 3)


@pytest.mark.parametrize("missing", ["session_id", "tool_input", "file_path"])
def test_read_without_identity_does_not_record_coverage(
    partial_read: dict[str, Any], missing: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hooks.context import file_read_cache

    recorded: list[tuple[Any, ...]] = []
    monkeypatch.setattr(file_read_cache, "mark_file_read", lambda *args: recorded.append(args))
    payload = dict(partial_read)
    if missing == "file_path":
        payload["tool_input"] = {}
    else:
        payload.pop(missing)
    file_read_cache.mark_read_result(payload)
    assert recorded == []


def test_redis_range_cache_uses_versioned_session_keys(partial_read: dict[str, Any]) -> None:
    import json

    from hooks._redis import redis_key
    from hooks.context import file_read_cache

    redis = file_read_cache.get_redis()
    if redis is None:
        return
    file_read_cache.mark_read_result(partial_read)
    path = partial_read["tool_input"]["file_path"]
    encoded = redis.hget(redis_key("file_read_ranges", partial_read["session_id"]), path)
    assert json.loads(encoded) == [[1, 402]]
    assert not file_read_cache.was_file_read("another-session", path, 1, 1)


@pytest.mark.parametrize("delta", [0.001, 0.01])
def test_range_revision_threshold_invalidates_cached_lines(
    partial_read: dict[str, Any], monkeypatch: pytest.MonkeyPatch, delta: float
) -> None:
    from hooks.context import file_read_cache

    monkeypatch.setattr(file_read_cache, "_get_mtime", lambda _: 0.0)
    file_read_cache.mark_read_result(partial_read)
    monkeypatch.setattr(file_read_cache, "_get_mtime", lambda _: delta)
    assert not file_read_cache.was_file_read(partial_read["session_id"], partial_read["tool_input"]["file_path"], 1, 1)
