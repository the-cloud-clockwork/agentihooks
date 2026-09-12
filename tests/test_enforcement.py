"""Tests for the enforcement drumbeat injection system."""

import json
import subprocess
from unittest.mock import patch

import pytest


def _git(*args: str, cwd) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture(autouse=True)
def _clean_enforcement(tmp_path):
    """Redirect enforcement state files to tmp dir; disable bundle/profile loading by default."""
    store = tmp_path / "enforcements.json"
    counters = tmp_path / "enforcement_counters.json"
    with (
        patch("hooks.context.enforcement._store_path", return_value=store),
        patch("hooks.context.enforcement._counter_path", return_value=counters),
        patch("hooks.context.enforcement._get_bundle_path", return_value=None),
        patch("hooks.context.enforcement._get_active_profile", return_value=None),
    ):
        yield


@pytest.fixture()
def bundle_dir(tmp_path):
    """Create a tmp bundle with enforcements.json + profile enforcements."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "enforcements.json").write_text(
        json.dumps({"enforcements": [{"id": "b-1", "message": "bundle msg", "cadence": 3, "tag": ""}]})
    )
    profile_dir = bundle / "profiles" / "testprofile"
    profile_dir.mkdir(parents=True)
    (profile_dir / "enforcements.json").write_text(
        json.dumps({"enforcements": [{"id": "p-1", "message": "profile msg", "cadence": 4, "tag": "doctrine"}]})
    )
    return bundle


@pytest.fixture
def local_repo(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    _git("init", "-q", cwd=root)
    return root


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
            ctx = get_pretool_enforcements("sess-1")  # count=3
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


class TestThreeSourceMerge:
    def test_bundle_only(self, bundle_dir):
        from hooks.context.enforcement import load_all_enforcements

        with (
            patch("hooks.context.enforcement._get_bundle_path", return_value=bundle_dir),
            patch("hooks.context.enforcement._get_active_profile", return_value=None),
        ):
            entries = load_all_enforcements()
            assert len(entries) == 1
            assert entries[0]["id"] == "b-1"
            assert entries[0]["source"] == "bundle"

    def test_profile_only(self, bundle_dir):
        from hooks.context.enforcement import load_all_enforcements

        empty_bundle = bundle_dir / "enforcements.json"
        empty_bundle.write_text(json.dumps({"enforcements": []}))
        with (
            patch("hooks.context.enforcement._get_bundle_path", return_value=bundle_dir),
            patch("hooks.context.enforcement._get_active_profile", return_value="testprofile"),
        ):
            entries = load_all_enforcements()
            assert len(entries) == 1
            assert entries[0]["id"] == "p-1"
            assert entries[0]["source"] == "profile"

    def test_runtime_only(self):
        from hooks.context.enforcement import add_enforcement, load_all_enforcements

        add_enforcement("runtime msg", 2)
        entries = load_all_enforcements()
        assert len(entries) == 1
        assert entries[0]["source"] == "runtime"

    def test_all_three_sources(self, bundle_dir):
        from hooks.context.enforcement import add_enforcement, load_all_enforcements

        add_enforcement("runtime msg", 2)
        with (
            patch("hooks.context.enforcement._get_bundle_path", return_value=bundle_dir),
            patch("hooks.context.enforcement._get_active_profile", return_value="testprofile"),
        ):
            entries = load_all_enforcements()
            sources = {e["source"] for e in entries}
            assert sources == {"bundle", "profile", "runtime"}
            assert len(entries) == 3

    def test_id_collision_runtime_wins(self, bundle_dir):
        from hooks.context.enforcement import load_all_enforcements

        (bundle_dir / "enforcements.json").write_text(
            json.dumps({"enforcements": [{"id": "clash", "message": "from bundle", "cadence": 5, "tag": ""}]})
        )
        profile_dir = bundle_dir / "profiles" / "testprofile"
        (profile_dir / "enforcements.json").write_text(
            json.dumps({"enforcements": [{"id": "clash", "message": "from profile", "cadence": 5, "tag": ""}]})
        )
        # Runtime also has same id — write directly to store
        from hooks.context.enforcement import _save_store

        _save_store([{"id": "clash", "message": "from runtime", "cadence": 5, "tag": ""}])

        with (
            patch("hooks.context.enforcement._get_bundle_path", return_value=bundle_dir),
            patch("hooks.context.enforcement._get_active_profile", return_value="testprofile"),
        ):
            entries = load_all_enforcements()
            assert len(entries) == 1
            assert entries[0]["message"] == "from runtime"
            assert entries[0]["source"] == "runtime"

    def test_id_collision_profile_wins_over_bundle(self, bundle_dir):
        from hooks.context.enforcement import load_all_enforcements

        (bundle_dir / "enforcements.json").write_text(
            json.dumps({"enforcements": [{"id": "clash", "message": "from bundle", "cadence": 5, "tag": ""}]})
        )
        profile_dir = bundle_dir / "profiles" / "testprofile"
        (profile_dir / "enforcements.json").write_text(
            json.dumps({"enforcements": [{"id": "clash", "message": "from profile", "cadence": 5, "tag": ""}]})
        )
        with (
            patch("hooks.context.enforcement._get_bundle_path", return_value=bundle_dir),
            patch("hooks.context.enforcement._get_active_profile", return_value="testprofile"),
        ):
            entries = load_all_enforcements()
            assert len(entries) == 1
            assert entries[0]["message"] == "from profile"
            assert entries[0]["source"] == "profile"

    def test_clear_only_touches_runtime(self, bundle_dir):
        from hooks.context.enforcement import add_enforcement, clear_enforcement, load_all_enforcements

        add_enforcement("runtime msg", 2)
        with (
            patch("hooks.context.enforcement._get_bundle_path", return_value=bundle_dir),
            patch("hooks.context.enforcement._get_active_profile", return_value="testprofile"),
        ):
            before = len(load_all_enforcements())
            clear_enforcement()
            after = load_all_enforcements()
            bundle_profile = [e for e in after if e["source"] in ("bundle", "profile")]
            runtime = [e for e in after if e["source"] == "runtime"]
            assert len(bundle_profile) == 2
            assert len(runtime) == 0
            assert len(after) == before - 1


class TestLocalEnforcement:
    def test_set_creates_project_store(self, local_repo):
        from hooks.context.enforcement import add_enforcement, list_enforcements

        enforcement_id = add_enforcement("project rule", 10, tag="canary", local=True, cwd=local_repo)
        store = local_repo / ".agentihooks" / "enforcements.json"
        assert store.is_file()
        entries = list_enforcements(local=True, cwd=local_repo)
        assert entries == [
            {
                "id": enforcement_id,
                "message": "project rule",
                "cadence": 10,
                "tag": "canary",
                "created_at": entries[0]["created_at"],
                "source": "local",
            }
        ]

    def test_list_and_clear_missing_store_do_not_create_it(self, local_repo):
        from hooks.context.enforcement import clear_enforcement, list_enforcements

        assert list_enforcements(local=True, cwd=local_repo) == []
        assert clear_enforcement(local=True, cwd=local_repo) == 0
        assert not (local_repo / ".agentihooks").exists()

    def test_local_clear_does_not_touch_runtime(self, local_repo):
        from hooks.context.enforcement import add_enforcement, clear_enforcement, list_enforcements

        add_enforcement("global", 5)
        add_enforcement("local", 10, local=True, cwd=local_repo)
        assert clear_enforcement(local=True, cwd=local_repo) == 1
        assert [entry["message"] for entry in list_enforcements()] == ["global"]
        assert list_enforcements(local=True, cwd=local_repo) == []

    def test_local_wins_id_collision(self, local_repo):
        from hooks.context.enforcement import _save_store, load_all_enforcements

        _save_store([{"id": "same", "message": "runtime", "cadence": 5, "tag": ""}])
        store = local_repo / ".agentihooks" / "enforcements.json"
        store.parent.mkdir()
        store.write_text(json.dumps({"enforcements": [{"id": "same", "message": "local", "cadence": 10, "tag": ""}]}))
        entries = load_all_enforcements(local_repo)
        assert len(entries) == 1
        assert entries[0]["message"] == "local"
        assert entries[0]["source"] == "local"

    def test_local_isolated_by_project(self, local_repo, tmp_path):
        from hooks.context.enforcement import add_enforcement, load_all_enforcements

        other = tmp_path / "other"
        other.mkdir()
        _git("init", "-q", cwd=other)
        add_enforcement("only here", 10, local=True, cwd=local_repo)
        assert [entry["message"] for entry in load_all_enforcements(local_repo)] == ["only here"]
        assert load_all_enforcements(other) == []

    def test_pretool_fires_local_at_tenth_call(self, local_repo):
        from hooks.context.enforcement import (
            add_enforcement,
            get_posttool_enforcements,
            get_pretool_enforcements,
        )

        add_enforcement("tenth-call canary", 10, local=True, cwd=local_repo)
        with patch("hooks.context.enforcement.ENFORCEMENT_INJECTION_ENABLED", True):
            for _ in range(9):
                assert get_pretool_enforcements("local-session", local_repo) is None
            assert get_posttool_enforcements("local-session", local_repo) is None
            banner = get_pretool_enforcements("local-session", local_repo)
            assert banner is not None
            assert "tenth-call canary" in banner
            assert "tenth-call canary" in get_posttool_enforcements("local-session", local_repo)
            for _ in range(9):
                assert get_pretool_enforcements("local-session", local_repo) is None
            assert "tenth-call canary" in get_pretool_enforcements("local-session", local_repo)

    def test_local_requires_git_project(self, tmp_path):
        from hooks.context.enforcement import add_enforcement
        from hooks.context.project_resources import ProjectResourceError

        with pytest.raises(ProjectResourceError, match="not inside a Git project"):
            add_enforcement("no project", 5, local=True, cwd=tmp_path)
