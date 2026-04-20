"""Tests for broadcast channels and brain adapter."""

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _clean_broadcast(tmp_path):
    """Redirect broadcast state to tmp dir."""
    bcast_file = tmp_path / "broadcast.json"
    sessions_file = tmp_path / "active-sessions.json"
    with (
        patch("hooks.context.broadcast._broadcast_path", return_value=bcast_file),
        patch("hooks.context.broadcast._sessions_path", return_value=sessions_file),
    ):
        yield


class TestChannelFiltering:
    def test_global_message_reaches_all(self):
        from hooks.context.broadcast import _message_matches_channel

        assert _message_matches_channel({"message": "global"}, []) is True
        assert _message_matches_channel({"message": "global"}, ["brain"]) is True
        assert _message_matches_channel({"message": "global"}, ["*"]) is True

    def test_channel_message_filtered(self):
        from hooks.context.broadcast import _message_matches_channel

        msg = {"message": "brain msg", "channel": "brain"}
        assert _message_matches_channel(msg, ["brain"]) is True
        assert _message_matches_channel(msg, ["ops"]) is False
        assert _message_matches_channel(msg, []) is False

    def test_wildcard_receives_all(self):
        from hooks.context.broadcast import _message_matches_channel

        msg = {"message": "brain msg", "channel": "brain"}
        assert _message_matches_channel(msg, ["*"]) is True

    def test_get_pending_filters_by_channel(self):
        from hooks.context.broadcast import create_broadcast, get_pending_broadcasts

        create_broadcast("global msg", severity="info")
        create_broadcast("brain msg", severity="info", channel="brain")
        create_broadcast("ops msg", severity="info", channel="ops")

        # Mock session channels
        with patch("hooks.context.broadcast._get_session_channels", return_value=["brain"]):
            pending = get_pending_broadcasts("sess-1")
        # Should get global + brain, not ops
        messages = [m["message"] for m in pending]
        assert "global msg" in messages
        assert "brain msg" in messages
        assert "ops msg" not in messages

    def test_no_channels_gets_only_global(self):
        from hooks.context.broadcast import create_broadcast, get_pending_broadcasts

        create_broadcast("global msg", severity="info")
        create_broadcast("brain msg", severity="info", channel="brain")

        with patch("hooks.context.broadcast._get_session_channels", return_value=[]):
            pending = get_pending_broadcasts("sess-2")
        messages = [m["message"] for m in pending]
        assert "global msg" in messages
        assert "brain msg" not in messages


class TestClearByChannel:
    def test_clear_specific_channel(self):
        from hooks.context.broadcast import _load_broadcasts, clear_broadcasts, create_broadcast

        create_broadcast("global msg", severity="info")
        create_broadcast("brain 1", severity="info", channel="brain")
        create_broadcast("brain 2", severity="info", channel="brain")
        create_broadcast("ops msg", severity="info", channel="ops")

        count = clear_broadcasts(channel="brain")
        assert count == 2
        remaining = _load_broadcasts()
        channels = [m.get("channel") for m in remaining]
        assert "brain" not in channels
        assert None in channels  # global still there
        assert "ops" in channels

    def test_clear_all(self):
        from hooks.context.broadcast import clear_broadcasts, create_broadcast

        create_broadcast("msg1", severity="info")
        create_broadcast("msg2", severity="info", channel="brain")
        count = clear_broadcasts()
        assert count == 2


class TestBrainAdapter:
    def test_file_brain_source_reads_files(self, tmp_path):
        from hooks.context.brain_adapter import FileBrainSource

        brain_dir = tmp_path / "brain"
        brain_dir.mkdir()
        (brain_dir / "hot-arcs.md").write_text("---\nid: hot\ntitle: Hot Arcs\npriority: 10\n---\nArc content here.")
        (brain_dir / "notes.md").write_text("---\nid: notes\ntitle: Notes\npriority: 5\n---\nSome notes.")

        source = FileBrainSource(brain_dir)
        entries = source.fetch()
        assert len(entries) == 2
        # Higher priority first
        assert entries[0].id == "hot"
        assert entries[0].priority == 10
        assert entries[1].id == "notes"
        assert "Arc content here" in entries[0].content

    def test_file_brain_source_empty_dir(self, tmp_path):
        from hooks.context.brain_adapter import FileBrainSource

        brain_dir = tmp_path / "empty_brain"
        brain_dir.mkdir()
        source = FileBrainSource(brain_dir)
        assert source.fetch() == []

    def test_file_brain_source_missing_dir(self, tmp_path):
        from hooks.context.brain_adapter import FileBrainSource

        source = FileBrainSource(tmp_path / "nonexistent")
        assert source.fetch() == []

    def test_frontmatter_parsing(self):
        from hooks.context.brain_adapter import _parse_frontmatter

        fm, body = _parse_frontmatter("---\nid: test\ntitle: Title\npriority: 7\n---\nBody text.")
        assert fm["id"] == "test"
        assert fm["title"] == "Title"
        assert fm["priority"] == "7"
        assert "Body text" in body

    def test_no_frontmatter(self):
        from hooks.context.brain_adapter import _parse_frontmatter

        fm, body = _parse_frontmatter("Just plain text.")
        assert fm == {}
        assert "Just plain text" in body

    def test_content_hash_changes(self):
        from hooks.context.brain_adapter import BrainEntry, _compute_hash

        e1 = [BrainEntry(id="a", title="A", content="hello", priority=5)]
        e2 = [BrainEntry(id="a", title="A", content="hello changed", priority=5)]
        assert _compute_hash(e1) != _compute_hash(e2)

    def test_content_hash_stable(self):
        from hooks.context.brain_adapter import BrainEntry, _compute_hash

        e1 = [BrainEntry(id="a", title="A", content="hello", priority=5)]
        assert _compute_hash(e1) == _compute_hash(e1)

    def test_get_status(self):
        with (
            patch("hooks.context.brain_adapter.BRAIN_ENABLED", True, create=True),
            patch("hooks.context.brain_adapter.BRAIN_SOURCE_TYPE", "file", create=True),
            patch("hooks.context.brain_adapter.BRAIN_SOURCE_PATH", "/tmp/brain", create=True),
            patch("hooks.context.brain_adapter.BRAIN_CHANNEL", "brain", create=True),
            patch("hooks.context.brain_adapter.BRAIN_REFRESH_INTERVAL", 30, create=True),
        ):
            # Patch the config imports inside get_status
            with patch.dict(
                "hooks.config.__dict__",
                {
                    "BRAIN_ENABLED": True,
                    "BRAIN_SOURCE_TYPE": "file",
                    "BRAIN_SOURCE_PATH": "/tmp/brain",
                    "BRAIN_CHANNEL": "brain",
                    "BRAIN_REFRESH_INTERVAL": 30,
                },
            ):
                from hooks.context.brain_adapter import get_status

                status = get_status()
                assert status["enabled"] is True
                assert status["source_type"] == "file"
