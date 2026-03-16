"""Tests for hooks.context.file_read_cache."""

import os
import tempfile
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

        with patch("hooks._redis.get_redis", return_value=None):
            mark_file_read(session_id, str(f))
            assert was_file_read(session_id, str(f)) is True

    def test_unread_file_returns_false(self, tmp_path):
        """was_file_read returns False for never-read file."""
        from hooks.context.file_read_cache import was_file_read

        f = tmp_path / "unread.py"
        f.write_text("content")

        with patch("hooks._redis.get_redis", return_value=None):
            assert was_file_read("session-xyz", str(f)) is False

    def test_mtime_change_allows_reread(self, tmp_path):
        """After marking, modifying file mtime → was_file_read returns False."""
        from hooks.context.file_read_cache import mark_file_read, was_file_read

        f = tmp_path / "modified.py"
        f.write_text("original content")
        session_id = "session-mtime"

        with patch("hooks._redis.get_redis", return_value=None):
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

        with patch("hooks._redis.get_redis", return_value=None):
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

        with patch("hooks._redis.get_redis", return_value=None):
            mark_file_read("session-A", str(f))
            assert was_file_read("session-A", str(f)) is True
            assert was_file_read("session-B", str(f)) is False

    def test_memory_fallback(self, tmp_path):
        """Redis unavailable → file_read_cache still works via memory dict."""
        from hooks.context.file_read_cache import mark_file_read, was_file_read

        f = tmp_path / "fallback.py"
        f.write_text("content")
        session_id = "session-fallback"

        with patch("hooks._redis.get_redis", return_value=None):
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

        with patch("hooks._redis.get_redis", return_value=None):
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

        with patch("hooks._redis.get_redis", return_value=None):
            # Should not raise
            check_and_block_redundant_read(payload)
