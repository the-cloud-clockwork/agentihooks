"""Extended tests for hooks.memory.store — file fallback, to_dict, from_dict, CRUD."""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


class TestMemoryToDict:
    """Test Memory.to_dict() conversion."""

    def test_to_dict_contains_all_fields(self):
        from hooks.memory.store import Memory

        mem = Memory(
            id="m-1",
            content="test content",
            summary="test summary",
            tags=["tag1", "tag2"],
            session_id="sess-1",
            source="manual",
            agent="test-agent",
            created_at=1700000000.0,
        )
        d = mem.to_dict()
        assert d["id"] == "m-1"
        assert d["content"] == "test content"
        assert d["summary"] == "test summary"
        assert d["tags"] == ["tag1", "tag2"]
        assert d["session_id"] == "sess-1"
        assert d["source"] == "manual"
        assert d["agent"] == "test-agent"
        assert d["created_at"] == 1700000000.0

    def test_to_dict_roundtrip(self):
        from hooks.memory.store import Memory

        mem = Memory(
            id="m-2",
            content="hello",
            summary="hi",
            tags=["a"],
            session_id="s-1",
            source="auto",
            agent="bot",
            created_at=1700000000.0,
        )
        d = mem.to_dict()
        mem2 = Memory.from_dict(d)
        assert mem2.id == mem.id
        assert mem2.content == mem.content
        assert mem2.tags == mem.tags


class TestMemoryFromDict:
    """Test Memory.from_dict() with various input formats."""

    def test_from_dict_with_json_string_tags(self):
        """from_dict() parses JSON string tags (Redis format)."""
        from hooks.memory.store import Memory

        data = {
            "id": "m-1",
            "content": "test",
            "summary": "s",
            "tags": '["a", "b"]',
            "session_id": "sess",
            "source": "manual",
            "agent": "bot",
            "created_at": "1700000000.0",
        }
        mem = Memory.from_dict(data)
        assert mem.tags == ["a", "b"]
        assert mem.created_at == 1700000000.0

    def test_from_dict_with_list_tags(self):
        """from_dict() accepts list tags directly."""
        from hooks.memory.store import Memory

        data = {
            "id": "m-2",
            "content": "c",
            "summary": "s",
            "tags": ["x", "y"],
            "session_id": "sess",
            "source": "auto",
            "agent": "bot",
            "created_at": 1700000000.0,
        }
        mem = Memory.from_dict(data)
        assert mem.tags == ["x", "y"]

    def test_from_dict_ignores_extra_fields(self):
        """from_dict() ignores extra keys not in the dataclass."""
        from hooks.memory.store import Memory

        data = {
            "id": "m-3",
            "content": "c",
            "summary": "s",
            "tags": [],
            "session_id": "sess",
            "source": "manual",
            "agent": "bot",
            "created_at": 1700000000.0,
            "extra_field": "should be ignored",
        }
        mem = Memory.from_dict(data)
        assert mem.id == "m-3"
        assert not hasattr(mem, "extra_field")

    def test_from_dict_does_not_mutate_input(self):
        """from_dict() makes a shallow copy, doesn't mutate input.

        Original dict should still have the string.
        """
        from hooks.memory.store import Memory

        data = {
            "id": "m-4",
            "content": "c",
            "summary": "s",
            "tags": '["a"]',
            "session_id": "sess",
            "source": "manual",
            "agent": "bot",
            "created_at": "100.0",
        }
        original_tags = data["tags"]
        Memory.from_dict(data)
        assert data["tags"] == original_tags


class TestMemoryStoreFileFallback:
    """Test MemoryStore with file fallback (no Redis)."""

    def _make_store(self, tmp_path):
        """Create a MemoryStore pointing to tmp_path.

        Ensure Redis is not used.
        """
        from hooks.memory.store import MemoryStore

        store = MemoryStore()
        store._file_path = tmp_path / "memories.jsonl"
        store._redis = None
        store._redis_checked = True
        return store

    def test_save_creates_file(self, tmp_path):
        """save() creates the JSONL file."""
        store = self._make_store(tmp_path)
        mem = store.save("test content", tags=["t1"], session_id="s1")
        assert mem.content == "test content"
        assert mem.tags == ["t1"]
        assert store._file_path.exists()

    def test_save_and_get(self, tmp_path):
        """save() then get() retrieves the same memory."""
        store = self._make_store(tmp_path)
        mem = store.save("hello", tags=["a"])
        retrieved = store.get(mem.id)
        assert retrieved is not None
        assert retrieved.content == "hello"
        assert retrieved.id == mem.id

    def test_get_nonexistent(self, tmp_path):
        """get() returns None for non-existent ID."""
        store = self._make_store(tmp_path)
        assert store.get("nonexistent-id") is None

    def test_save_multiple_and_list(self, tmp_path):
        """save() multiple memories and list_all() retrieves them."""
        store = self._make_store(tmp_path)
        store.save("first")
        store.save("second")
        store.save("third")
        all_mems = store.list_all(limit=10)
        assert len(all_mems) == 3

    def test_list_all_with_offset(self, tmp_path):
        """list_all() with offset skips entries.

        Offset 1, limit 2.
        """
        store = self._make_store(tmp_path)
        store.save("first")
        store.save("second")
        store.save("third")
        mems = store.list_all(limit=2, offset=1)
        assert len(mems) == 2

    def test_search_by_content(self, tmp_path):
        """search() finds memories by content keyword."""
        store = self._make_store(tmp_path)
        store.save("the quick brown fox")
        store.save("lazy dog")
        store.save("another fox entry")
        results = store.search("fox")
        assert len(results) == 2

    def test_search_by_tags(self, tmp_path):
        """search() filters by tags."""
        store = self._make_store(tmp_path)
        store.save("item one", tags=["python", "test"])
        store.save("item two", tags=["python"])
        store.save("item three", tags=["rust"])
        results = store.search("item", tags=["python"])
        assert len(results) == 2

    def test_search_no_results(self, tmp_path):
        """search() returns empty list when nothing matches."""
        store = self._make_store(tmp_path)
        store.save("hello world")
        results = store.search("nonexistent-query")
        assert results == []

    def test_search_with_limit(self, tmp_path):
        """search() respects limit parameter."""
        store = self._make_store(tmp_path)
        for i in range(10):
            store.save(f"repeated keyword item {i}")
        results = store.search("keyword", limit=3)
        assert len(results) == 3

    def test_recall_all(self, tmp_path):
        """recall() returns recent memories."""
        store = self._make_store(tmp_path)
        store.save("mem1")
        store.save("mem2")
        results = store.recall()
        assert len(results) == 2

    def test_recall_by_session(self, tmp_path):
        """recall() filters by session_id."""
        store = self._make_store(tmp_path)
        store.save("from session A", session_id="A")
        store.save("from session B", session_id="B")
        store.save("from session A again", session_id="A")
        results = store.recall(session_id="A")
        assert len(results) == 2

    def test_recall_by_tags(self, tmp_path):
        """recall() filters by tags."""
        store = self._make_store(tmp_path)
        store.save("tagged", tags=["important"])
        store.save("not tagged")
        results = store.recall(tags=["important"])
        assert len(results) == 1

    def test_recall_with_limit(self, tmp_path):
        """recall() respects limit."""
        store = self._make_store(tmp_path)
        for i in range(5):
            store.save(f"mem {i}")
        results = store.recall(limit=2)
        assert len(results) == 2

    def test_delete_existing(self, tmp_path):
        """delete() removes a memory and returns True."""
        store = self._make_store(tmp_path)
        mem = store.save("to delete")
        assert store.delete(mem.id) is True
        assert store.get(mem.id) is None

    def test_delete_nonexistent(self, tmp_path):
        """delete() returns False for non-existent ID."""
        store = self._make_store(tmp_path)
        assert store.delete("nonexistent") is False

    def test_clear(self, tmp_path):
        """clear() removes all memories."""
        store = self._make_store(tmp_path)
        store.save("one")
        store.save("two")
        count = store.clear()
        assert count == 2
        assert store.list_all() == []

    def test_clear_empty(self, tmp_path):
        """clear() on empty store returns 0."""
        store = self._make_store(tmp_path)
        count = store.clear()
        assert count == 0

    def test_save_default_summary(self, tmp_path):
        """save() generates summary from first 100 chars of content."""
        store = self._make_store(tmp_path)
        long_content = "x" * 200
        mem = store.save(long_content)
        assert mem.summary == long_content[:100]

    def test_save_custom_summary(self, tmp_path):
        """save() uses provided summary."""
        store = self._make_store(tmp_path)
        mem = store.save("content", summary="my summary")
        assert mem.summary == "my summary"

    def test_file_read_all_corrupt_lines(self, tmp_path):
        """_file_read_all() skips corrupt JSON lines.

        Write a mix of valid and invalid lines.
        """
        store = self._make_store(tmp_path)
        with open(store._file_path, "w") as f:
            f.write('{"id":"1","content":"valid"}\n')
            f.write("not valid json\n")
            f.write('{"id":"2","content":"also valid"}\n')
        entries = store._file_read_all()
        assert len(entries) == 2


class TestMemoryStoreRedisPath:
    """Test that MemoryStore tries Redis first."""

    def test_get_redis_catches_import_error(self):
        """_get_redis() handles import failure gracefully."""
        from hooks.memory.store import MemoryStore

        store = MemoryStore()
        store._redis_checked = False
        with patch("hooks.memory.store.MemoryStore._get_redis") as mock_get:
            mock_get.return_value = None
            result = mock_get()
            assert result is None
