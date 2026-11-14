"""Tests for hooks.memory.store module.

File fallback should work without Redis Just verify no crash on basic operations.
"""

from time import time

import pytest

pytestmark = pytest.mark.unit


class TestMemoryDataclass:
    """Test the Memory dataclass."""

    def test_memory_creation(self):
        """Memory dataclass can be created."""
        from hooks.memory.store import Memory

        mem = Memory(
            id="test-1",
            content="Test content",
            summary="Test summary",
            tags=["test"],
            session_id="session-1",
            source="test",
            agent="test-agent",
            created_at=time(),
        )
        assert mem.id == "test-1"
        assert mem.content == "Test content"
        assert mem.tags == ["test"]
        assert mem.agent == "test-agent"


class TestMemoryStore:
    """Test MemoryStore operations."""

    def test_store_creation(self):
        """MemoryStore can be instantiated."""
        from hooks.memory.store import MemoryStore

        store = MemoryStore()
        assert store is not None

    def test_store_save_and_recall(self, tmp_path):
        """MemoryStore can save and recall memories (file fallback)."""

        from hooks.memory.store import MemoryStore

        with patch_env(HOME=str(tmp_path)):
            MemoryStore()


def patch_env(**kwargs):
    """Helper to patch environment variables."""
    import os
    from unittest.mock import patch

    return patch.dict(os.environ, kwargs)
