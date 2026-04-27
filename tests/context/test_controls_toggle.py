"""Tests for hooks.context.controls_toggle and its integration with the gates."""

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _no_redis():
    with (
        patch("hooks._redis.get_redis", return_value=None),
        patch("hooks.context.controls_toggle.get_redis", return_value=None),
        patch("hooks.context.branch_guard.get_redis", return_value=None),
        patch("hooks.context.prod_lockdown.get_redis", return_value=None),
    ):
        yield


@pytest.fixture(autouse=True)
def _isolated_flag(tmp_path, monkeypatch):
    flag_dir = tmp_path / "controls_flags"
    monkeypatch.setattr("hooks.context.controls_toggle._FLAG_DIR", flag_dir)
    monkeypatch.setattr("hooks.context.controls_toggle._GLOBAL_FLAG", flag_dir / "active.flag")
    yield


class TestSignalDetection:
    def test_disable_basic(self):
        from hooks.context.controls_toggle import contains_disable_signal

        assert contains_disable_signal("disable controls")
        assert contains_disable_signal("Please DISABLE Controls now")
        assert contains_disable_signal("turn off controls for this session")
        assert contains_disable_signal("deactivate controls")

    def test_enable_basic(self):
        from hooks.context.controls_toggle import contains_enable_signal

        assert contains_enable_signal("enable controls")
        assert contains_enable_signal("turn on controls")
        assert contains_enable_signal("activate controls")
        assert contains_enable_signal("restore controls")

    def test_no_false_positive(self):
        from hooks.context.controls_toggle import (
            contains_disable_signal,
            contains_enable_signal,
        )

        assert not contains_disable_signal("the disable button is broken")
        assert not contains_enable_signal("controls feel enabled")
        assert not contains_disable_signal("")
        assert not contains_enable_signal("")


class TestSetClear:
    def test_set_and_check(self):
        from hooks.context.controls_toggle import (
            is_controls_disabled,
            set_controls_disabled,
        )

        assert not is_controls_disabled()
        set_controls_disabled("sess-1")
        assert is_controls_disabled()
        assert is_controls_disabled("anyone")

    def test_subagent_inherits(self):
        from hooks.context.controls_toggle import (
            is_controls_disabled,
            set_controls_disabled,
        )

        set_controls_disabled("parent-sess")
        assert is_controls_disabled("child-sess")
        assert is_controls_disabled(None)

    def test_subagent_clear_is_noop(self):
        from hooks.context.controls_toggle import (
            clear_controls_disabled,
            is_controls_disabled,
            set_controls_disabled,
        )

        set_controls_disabled("parent")
        clear_controls_disabled("child-sess")
        assert is_controls_disabled()

    def test_owner_clear_works(self):
        from hooks.context.controls_toggle import (
            clear_controls_disabled,
            is_controls_disabled,
            set_controls_disabled,
        )

        set_controls_disabled("parent")
        clear_controls_disabled("parent")
        assert not is_controls_disabled()

    def test_force_clear(self):
        from hooks.context.controls_toggle import (
            clear_controls_disabled,
            is_controls_disabled,
            set_controls_disabled,
        )

        set_controls_disabled("parent")
        clear_controls_disabled("anyone", force=True)
        assert not is_controls_disabled()


class TestBranchGuardIntegration:
    def _check(self, command: str, sid: str = "ctl-test-branch-uniq"):
        from hooks.context.branch_guard import check_branch_guard

        check_branch_guard({"tool_input": {"command": command}, "session_id": sid})

    def test_branch_create_blocked_normally(self):
        from hooks.hook_manager import BlockAction

        with pytest.raises(BlockAction):
            self._check("git checkout -b feat/x")

    def test_branch_create_allowed_with_bypass(self):
        from hooks.context.controls_toggle import set_controls_disabled

        set_controls_disabled("test")
        self._check("git checkout -b feat/x")

    def test_pr_create_allowed_with_bypass(self):
        from hooks.context.controls_toggle import set_controls_disabled

        set_controls_disabled("test")
        self._check("gh pr create --base main --title t --body b")

    def test_pr_base_main_still_required_under_bypass(self):
        from hooks.context.controls_toggle import set_controls_disabled
        from hooks.hook_manager import BlockAction

        set_controls_disabled("test")
        with pytest.raises(BlockAction):
            self._check("gh pr create --base dev --title t --body b")

    def test_direct_push_main_still_blocked(self):
        from hooks.context.controls_toggle import set_controls_disabled
        from hooks.hook_manager import BlockAction

        set_controls_disabled("test")
        with pytest.raises(BlockAction):
            self._check("git push origin main")

    def test_force_push_blocked_normally(self):
        from hooks.hook_manager import BlockAction

        with pytest.raises(BlockAction):
            self._check("git push --force origin feat/x")

    def test_force_push_allowed_with_bypass(self):
        from hooks.context.controls_toggle import set_controls_disabled

        set_controls_disabled("test")
        self._check("git push --force origin feat/x")
        self._check("git push -f origin feat/x")
        self._check("git push --force-with-lease origin feat/x")

    def test_force_push_to_main_still_blocked(self):
        from hooks.context.controls_toggle import set_controls_disabled
        from hooks.hook_manager import BlockAction

        set_controls_disabled("test")
        with pytest.raises(BlockAction):
            self._check("git push --force origin main")

    def test_subagent_inherits_pr_unlock(self):
        from hooks.context.controls_toggle import set_controls_disabled

        set_controls_disabled("parent-sess")
        self._check(
            "gh pr create --base main --title t --body b",
            sid="child-sess",
        )


class TestProdLockdownIntegration:
    def _check(self, command: str, sid: str = "ctl-test-prodlock-uniq"):
        from hooks.context.prod_lockdown import check_prod_lockdown

        check_prod_lockdown({"tool_input": {"command": command}, "session_id": sid})

    def test_release_workflow_blocked_normally(self):
        from hooks.hook_manager import BlockAction

        with pytest.raises(BlockAction):
            self._check("gh workflow run release.yml")

    def test_release_workflow_allowed_with_bypass(self):
        from hooks.context.controls_toggle import set_controls_disabled

        set_controls_disabled("test")
        self._check("gh workflow run release.yml")

    def test_pr_merge_main_allowed_with_bypass(self):
        from hooks.context.controls_toggle import set_controls_disabled

        set_controls_disabled("test")
        self._check("gh pr merge 123 --base main")

    def test_latest_image_push_allowed_with_bypass(self):
        from hooks.context.controls_toggle import set_controls_disabled

        set_controls_disabled("test")
        self._check("docker push ghcr.io/anton/agent:latest")
