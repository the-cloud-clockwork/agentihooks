"""Tests for hooks.context.context_refresh."""

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _no_redis():
    with patch("hooks.context.context_refresh.get_redis", return_value=None):
        yield


@pytest.fixture()
def tmp_home(tmp_path):
    with patch("hooks.context.context_refresh._state_file") as mock_sf:
        def _sf(session_id):
            safe = session_id.replace("/", "_")
            return tmp_path / f"ctx_refresh_{safe}.json"
        mock_sf.side_effect = _sf
        yield tmp_path


@pytest.fixture()
def rules_dir(tmp_path):
    d = tmp_path / "rules"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# Turn counter state
# ---------------------------------------------------------------------------


class TestTurnCounter:
    def test_default_state(self, tmp_home):
        from hooks.context.context_refresh import _get_state

        state = _get_state("test-session-1")
        assert state == {"turn_count": 0, "last_refresh": 0, "last_claude_md_refresh": 0}

    def test_persist_across_calls(self, tmp_home):
        from hooks.context.context_refresh import _get_state, _set_state

        _set_state("s1", {"turn_count": 5, "last_refresh": 0, "last_claude_md_refresh": 0})
        assert _get_state("s1") == {"turn_count": 5, "last_refresh": 0, "last_claude_md_refresh": 0}

    def test_sessions_isolated(self, tmp_home):
        from hooks.context.context_refresh import _get_state, _set_state

        _set_state("s1", {"turn_count": 10, "last_refresh": 0, "last_claude_md_refresh": 0})
        _set_state("s2", {"turn_count": 3, "last_refresh": 0, "last_claude_md_refresh": 0})
        assert _get_state("s1")["turn_count"] == 10
        assert _get_state("s2")["turn_count"] == 3


# ---------------------------------------------------------------------------
# Rules loading
# ---------------------------------------------------------------------------


class TestRulesLoading:
    def test_loads_md_files(self, rules_dir):
        (rules_dir / "a.md").write_text("rule A")
        (rules_dir / "b.md").write_text("rule B")

        from hooks.context.context_refresh import _load_rules_files

        result = _load_rules_files(str(rules_dir), include_project=False)
        assert len(result) == 2
        assert result[0] == ("a.md", "rule A")
        assert result[1] == ("b.md", "rule B")

    def test_frontmatter_stripped(self, rules_dir):
        (rules_dir / "r.md").write_text("---\ndescription: test\nalwaysApply: true\n---\nactual rule")

        from hooks.context.context_refresh import _load_rules_files

        result = _load_rules_files(str(rules_dir), include_project=False)
        assert result[0][1] == "actual rule"

    def test_no_frontmatter_passes_through(self, rules_dir):
        (rules_dir / "r.md").write_text("plain rule content")

        from hooks.context.context_refresh import _load_rules_files

        result = _load_rules_files(str(rules_dir), include_project=False)
        assert result[0][1] == "plain rule content"

    def test_missing_dir_returns_empty(self):
        from hooks.context.context_refresh import _load_rules_files

        result = _load_rules_files("/nonexistent/path", include_project=False)
        assert result == []

    def test_empty_file_skipped(self, rules_dir):
        (rules_dir / "empty.md").write_text("")
        (rules_dir / "real.md").write_text("content")

        from hooks.context.context_refresh import _load_rules_files

        result = _load_rules_files(str(rules_dir), include_project=False)
        assert len(result) == 1
        assert result[0][0] == "real.md"

    def test_readme_skipped(self, rules_dir):
        (rules_dir / "README.md").write_text("readme")
        (rules_dir / "a.md").write_text("rule")

        from hooks.context.context_refresh import _load_rules_files

        result = _load_rules_files(str(rules_dir), include_project=False)
        assert len(result) == 1
        assert result[0][0] == "a.md"

    def test_project_rules_included(self, rules_dir, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        proj_rules = tmp_path / ".claude" / "rules"
        proj_rules.mkdir(parents=True)
        (proj_rules / "proj.md").write_text("project rule")
        (rules_dir / "global.md").write_text("global rule")

        from hooks.context.context_refresh import _load_rules_files

        result = _load_rules_files(str(rules_dir), include_project=True)
        names = [r[0] for r in result]
        assert "global.md" in names
        assert "proj.md" in names

    def test_project_rules_excluded(self, rules_dir, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        proj_rules = tmp_path / ".claude" / "rules"
        proj_rules.mkdir(parents=True)
        (proj_rules / "proj.md").write_text("project rule")
        (rules_dir / "global.md").write_text("global rule")

        from hooks.context.context_refresh import _load_rules_files

        result = _load_rules_files(str(rules_dir), include_project=False)
        names = [r[0] for r in result]
        assert "global.md" in names
        assert "proj.md" not in names


# ---------------------------------------------------------------------------
# Content cap
# ---------------------------------------------------------------------------


class TestContentCap:
    def test_cap_truncates(self, rules_dir):
        for i in range(20):
            (rules_dir / f"rule_{i:02d}.md").write_text("x" * 1000)

        from hooks.context.context_refresh import _build_injection_text, _load_rules_files

        rules = _load_rules_files(str(rules_dir), include_project=False)
        with patch("hooks.config.CONTEXT_REFRESH_MAX_CHARS", 3000):
            text = _build_injection_text(rules, 20, 20)
        assert "omitted" in text
        assert len(text) < 4000


# ---------------------------------------------------------------------------
# maybe_refresh integration
# ---------------------------------------------------------------------------


class TestMaybeRefresh:
    def test_no_injection_before_interval(self, tmp_home, rules_dir):
        (rules_dir / "a.md").write_text("rule")

        with patch("hooks.config.CONTEXT_REFRESH_ENABLED", True), \
             patch("hooks.config.CONTEXT_REFRESH_INTERVAL", 20), \
             patch("hooks.config.CONTEXT_REFRESH_RULES_DIR", str(rules_dir)), \
             patch("hooks.config.CONTEXT_REFRESH_INCLUDE_PROJECT", False), \
             patch("hooks.config.CONTEXT_REFRESH_CLAUDE_MD_INTERVAL", 0), \
             patch("hooks.common.inject_banner") as mock_banner:

            from hooks.context.context_refresh import maybe_refresh

            for _ in range(19):
                maybe_refresh("test-sess")

            mock_banner.assert_not_called()

    def test_injection_at_interval(self, tmp_home, rules_dir):
        (rules_dir / "a.md").write_text("rule A")

        with patch("hooks.config.CONTEXT_REFRESH_ENABLED", True), \
             patch("hooks.config.CONTEXT_REFRESH_INTERVAL", 5), \
             patch("hooks.config.CONTEXT_REFRESH_RULES_DIR", str(rules_dir)), \
             patch("hooks.config.CONTEXT_REFRESH_INCLUDE_PROJECT", False), \
             patch("hooks.config.CONTEXT_REFRESH_MAX_CHARS", 8000), \
             patch("hooks.config.CONTEXT_REFRESH_CLAUDE_MD_INTERVAL", 0), \
             patch("hooks.common.inject_banner") as mock_banner:

            from hooks.context.context_refresh import maybe_refresh

            for _ in range(5):
                maybe_refresh("test-sess-2")

            mock_banner.assert_called_once()
            call_args = mock_banner.call_args
            assert "turn 5" in call_args[0][0]

    def test_injection_recurs(self, tmp_home, rules_dir):
        (rules_dir / "a.md").write_text("rule")

        with patch("hooks.config.CONTEXT_REFRESH_ENABLED", True), \
             patch("hooks.config.CONTEXT_REFRESH_INTERVAL", 5), \
             patch("hooks.config.CONTEXT_REFRESH_RULES_DIR", str(rules_dir)), \
             patch("hooks.config.CONTEXT_REFRESH_INCLUDE_PROJECT", False), \
             patch("hooks.config.CONTEXT_REFRESH_MAX_CHARS", 8000), \
             patch("hooks.config.CONTEXT_REFRESH_CLAUDE_MD_INTERVAL", 0), \
             patch("hooks.common.inject_banner") as mock_banner:

            from hooks.context.context_refresh import maybe_refresh

            for _ in range(10):
                maybe_refresh("test-sess-3")

            assert mock_banner.call_count == 2

    def test_disabled_skips(self, tmp_home):
        with patch("hooks.config.CONTEXT_REFRESH_ENABLED", False), \
             patch("hooks.common.inject_banner") as mock_banner:

            from hooks.context.context_refresh import maybe_refresh

            maybe_refresh("test-sess-4")
            mock_banner.assert_not_called()

    def test_empty_session_id_noop(self):
        from hooks.context.context_refresh import maybe_refresh

        maybe_refresh("")  # should not raise

    def test_empty_rules_no_injection(self, tmp_home, rules_dir):
        # rules_dir exists but has no .md files

        with patch("hooks.config.CONTEXT_REFRESH_ENABLED", True), \
             patch("hooks.config.CONTEXT_REFRESH_INTERVAL", 1), \
             patch("hooks.config.CONTEXT_REFRESH_RULES_DIR", str(rules_dir)), \
             patch("hooks.config.CONTEXT_REFRESH_INCLUDE_PROJECT", False), \
             patch("hooks.common.inject_banner") as mock_banner:

            from hooks.context.context_refresh import maybe_refresh

            maybe_refresh("test-sess-5")
            mock_banner.assert_not_called()


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


class TestClearSessionState:
    def test_clear_removes_file(self, tmp_home):
        from hooks.context.context_refresh import _set_state, clear_session_state

        _set_state("s1", {"turn_count": 10, "last_refresh": 5, "last_claude_md_refresh": 0})
        clear_session_state("s1")

        from hooks.context.context_refresh import _get_state

        assert _get_state("s1") == {"turn_count": 0, "last_refresh": 0, "last_claude_md_refresh": 0}

    def test_clear_nonexistent_safe(self):
        from hooks.context.context_refresh import clear_session_state

        clear_session_state("nonexistent")  # should not raise

    def test_clear_empty_session_id(self):
        from hooks.context.context_refresh import clear_session_state

        clear_session_state("")  # should not raise
