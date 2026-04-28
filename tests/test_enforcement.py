"""Tests for the enforcement drumbeat injection system."""

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _clean_enforcement(tmp_path):
    """Redirect enforcement state files to tmp dir."""
    store = tmp_path / "enforcements.json"
    counters = tmp_path / "enforcement_counters.json"
    with (
        patch("hooks.context.enforcement._store_path", return_value=store),
        patch("hooks.context.enforcement._counter_path", return_value=counters),
    ):
        yield


class TestCRUD:
    def test_add_returns_id(self):
        from hooks.context.enforcement import add_enforcement, list_enforcements

        eid = add_enforcement("no patches", 5)
        assert eid is not None
        entries = list_enforcements()
        assert len(entries) == 1
        assert entries[0]["message"] == "no patches"
        assert entries[0]["cadence"] == 5
        assert entries[0]["id"] == eid

    def test_add_rejects_empty(self):
        from hooks.context.enforcement import add_enforcement

        assert add_enforcement("", 5) is None
        assert add_enforcement("   ", 5) is None

    def test_add_rejects_invalid_cadence(self):
        from hooks.context.enforcement import add_enforcement

        assert add_enforcement("msg", 0) is None
        assert add_enforcement("msg", -1) is None

    def test_clear_by_id(self):
        from hooks.context.enforcement import add_enforcement, clear_enforcement, list_enforcements

        eid1 = add_enforcement("a", 5)
        add_enforcement("b", 3)
        removed = clear_enforcement(enforcement_id=eid1)
        assert removed == 1
        entries = list_enforcements()
        assert len(entries) == 1
        assert entries[0]["message"] == "b"

    def test_clear_by_tag(self):
        from hooks.context.enforcement import add_enforcement, clear_enforcement, list_enforcements

        add_enforcement("a", 5, tag="rule")
        add_enforcement("b", 3, tag="rule")
        add_enforcement("c", 2, tag="other")
        removed = clear_enforcement(tag="rule")
        assert removed == 2
        entries = list_enforcements()
        assert len(entries) == 1
        assert entries[0]["tag"] == "other"

    def test_clear_all(self):
        from hooks.context.enforcement import add_enforcement, clear_enforcement, list_enforcements

        add_enforcement("a", 5)
        add_enforcement("b", 3)
        removed = clear_enforcement()
        assert removed == 2
        assert list_enforcements() == []


class TestCadence:
    def test_due_at_exact_multiple(self):
        from hooks.context.enforcement import add_enforcement, get_due_enforcements

        add_enforcement("five", 5)
        assert get_due_enforcements(4) == []
        due = get_due_enforcements(5)
        assert len(due) == 1
        assert get_due_enforcements(6) == []
        assert len(get_due_enforcements(10)) == 1

    def test_zero_count_never_fires(self):
        from hooks.context.enforcement import add_enforcement, get_due_enforcements

        add_enforcement("msg", 1)
        assert get_due_enforcements(0) == []

    def test_multiple_same_cadence_stack(self):
        from hooks.context.enforcement import add_enforcement, get_due_enforcements

        add_enforcement("a", 3)
        add_enforcement("b", 3)
        due = get_due_enforcements(3)
        assert len(due) == 2

    def test_multiple_different_cadence_independent(self):
        from hooks.context.enforcement import add_enforcement, get_due_enforcements

        add_enforcement("two", 2)
        add_enforcement("three", 3)
        assert {m["message"] for m in get_due_enforcements(2)} == {"two"}
        assert {m["message"] for m in get_due_enforcements(3)} == {"three"}
        assert {m["message"] for m in get_due_enforcements(6)} == {"two", "three"}


class TestCounter:
    def test_increment_per_session(self):
        from hooks.context.enforcement import increment_and_get_count

        assert increment_and_get_count("sess-A") == 1
        assert increment_and_get_count("sess-A") == 2
        assert increment_and_get_count("sess-B") == 1
        assert increment_and_get_count("sess-A") == 3

    def test_reset_session(self):
        from hooks.context.enforcement import increment_and_get_count, reset_session_counter

        increment_and_get_count("sess-X")
        increment_and_get_count("sess-X")
        reset_session_counter("sess-X")
        assert increment_and_get_count("sess-X") == 1


class TestPretoolEntry:
    def test_returns_none_when_no_enforcements(self):
        from hooks.context.enforcement import get_pretool_enforcements

        with patch("hooks.context.enforcement.ENFORCEMENT_INJECTION_ENABLED", True):
            for _ in range(10):
                assert get_pretool_enforcements("sess-1") is None

    def test_fires_at_cadence(self):
        from hooks.context.enforcement import add_enforcement, get_pretool_enforcements

        add_enforcement("no patches — code only", 3)
        with patch("hooks.context.enforcement.ENFORCEMENT_INJECTION_ENABLED", True):
            assert get_pretool_enforcements("sess-1") is None  # count=1
            assert get_pretool_enforcements("sess-1") is None  # count=2
            ctx = get_pretool_enforcements("sess-1")            # count=3
            assert ctx is not None
            assert "no patches — code only" in ctx
            assert "ENFORCEMENT" in ctx
            assert "IMPORTANT" in ctx
            assert get_pretool_enforcements("sess-1") is None  # count=4
            assert get_pretool_enforcements("sess-1") is None  # count=5
            assert get_pretool_enforcements("sess-1") is not None  # count=6

    def test_disabled_returns_none(self):
        from hooks.context.enforcement import add_enforcement, get_pretool_enforcements

        add_enforcement("msg", 1)
        with patch("hooks.context.enforcement.ENFORCEMENT_INJECTION_ENABLED", False):
            assert get_pretool_enforcements("sess-1") is None


class TestBannerFormat:
    def test_banner_contains_required_fields(self):
        from hooks.context.enforcement import format_enforcement_banner

        msg = {"id": "abc12345", "message": "do not patch", "cadence": 5, "tag": "no-patch"}
        banner = format_enforcement_banner(msg)
        assert "ENFORCEMENT" in banner
        assert "IMPORTANT" in banner
        assert "abc12345" in banner
        assert "do not patch" in banner
        assert "no-patch" in banner
        assert "every 5 tool calls" in banner

    def test_banner_omits_empty_tag(self):
        from hooks.context.enforcement import format_enforcement_banner

        msg = {"id": "x", "message": "m", "cadence": 1, "tag": ""}
        banner = format_enforcement_banner(msg)
        assert "Tag:" not in banner
