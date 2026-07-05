"""Tests for hooks.context.branch_guard."""

import pytest


class TestBranchGuard:
    """Test that git commands targeting main/master are blocked."""

    def _check(self, command: str):
        from hooks.context.branch_guard import check_branch_guard

        payload = {"tool_input": {"command": command}, "session_id": "test"}
        check_branch_guard(payload)

    def _assert_blocked(self, command: str):
        from hooks.hook_manager import BlockAction

        with pytest.raises(BlockAction):
            self._check(command)

    def _assert_allowed(self, command: str):
        self._check(command)  # should not raise

    # --- Blocked commands ---

    def test_push_main(self):
        self._assert_blocked("git push origin main")

    def test_push_master(self):
        self._assert_blocked("git push origin master")

    def test_push_head_main(self):
        self._assert_blocked("git push origin HEAD:main")

    def test_push_head_master(self):
        self._assert_blocked("git push origin HEAD:master")

    def test_checkout_main(self):
        self._assert_allowed("git checkout main")

    def test_switch_master(self):
        self._assert_allowed("git switch master")

    def test_merge_main(self):
        self._assert_blocked("git merge main")

    def test_rebase_main(self):
        self._assert_blocked("git rebase main")

    def test_reset_main(self):
        self._assert_blocked("git reset --hard main")

    def test_force_push(self):
        self._assert_blocked("git push --force origin dev")

    def test_force_push_short(self):
        self._assert_blocked("git push -f origin dev")

    def test_force_with_lease(self):
        self._assert_blocked("git push --force-with-lease origin dev")

    def test_branch_delete_main(self):
        self._assert_blocked("git branch -D main")

    def test_gh_pr_merge(self):
        self._assert_allowed("gh pr merge 123")

    # --- Allowed commands ---

    def test_push_head_allowed(self):
        self._assert_allowed("git push origin HEAD")

    def test_push_dev(self):
        self._assert_allowed("git push origin dev")

    def test_push_feature_branch(self):
        self._assert_allowed("git push origin feature/my-branch")

    def test_checkout_dev(self):
        self._assert_allowed("git checkout dev")

    def test_checkout_feature_blocked_without_signal(self):
        self._assert_blocked("git checkout -b feature/new-thing")

    def test_checkout_feature_allowed_with_signal(self):
        from unittest.mock import patch

        with patch("hooks.context.branch_guard._has_branch_signal", return_value=True):
            self._assert_allowed("git checkout -b feature/new-thing")

    def test_merge_dev(self):
        self._assert_allowed("git merge dev")

    def test_commit(self):
        self._assert_allowed("git commit -m 'fix something'")

    def test_status(self):
        self._assert_allowed("git status")

    def test_diff(self):
        self._assert_allowed("git diff main")  # reading, not writing

    def test_log_main(self):
        self._assert_allowed("git log main..HEAD")

    def test_non_git(self):
        self._assert_allowed("ls -la")

    def test_empty_command(self):
        self._assert_allowed("")

    def test_pull_main_allowed(self):
        """git pull from main is reading, not destructive."""
        self._assert_allowed("git pull origin main")

    def test_commit_message_with_main_allowed(self):
        """Commit messages mentioning main/master should not trigger the guard."""
        self._assert_allowed('git commit -m "fix: block operations targeting main/master"')

    def test_heredoc_commit_with_main_allowed(self):
        """Heredoc commit messages mentioning main should not trigger."""
        cmd = """git commit -m "$(cat <<'EOF'\nfeat: block git push to main\n\nCo-Authored-By: test\nEOF\n)" """
        self._assert_allowed(cmd)

    def test_echo_with_main_allowed(self):
        """Echo commands mentioning main should not trigger."""
        self._assert_allowed("echo 'do not push to main'")


class TestPRBaseGuard:
    """gh pr create must target dev explicitly; main/master and bare-default are
    blocked (CI Manifesto §4/§5 — there is no dev→main PR flow)."""

    def _check(self, command: str):
        from hooks.context.branch_guard import check_branch_guard

        payload = {"tool_input": {"command": command}, "session_id": "test-pr"}
        check_branch_guard(payload)

    def _signal_patches(self):
        from unittest.mock import patch

        return (
            patch("hooks.context.branch_guard._has_pr_signal", return_value=True),
            patch("hooks.context.branch_guard._get_pr_counter", return_value=0),
            patch("hooks.context.branch_guard.increment_pr_counter", return_value=1),
        )

    def _assert_blocked_with_signal(self, command: str):
        from hooks.hook_manager import BlockAction

        s1, s2, s3 = self._signal_patches()
        with s1, s2, s3, pytest.raises(BlockAction):
            self._check(command)

    def _assert_allowed_with_signal(self, command: str):
        s1, s2, s3 = self._signal_patches()
        with s1, s2, s3:
            self._check(command)  # should not raise

    def test_pr_base_main_blocked(self):
        self._assert_blocked_with_signal("gh pr create --base main --fill")

    def test_pr_base_master_blocked(self):
        self._assert_blocked_with_signal("gh pr create --base master -t x")

    def test_pr_bare_create_blocked(self):
        # a bare create defaults to the repo default branch (main)
        self._assert_blocked_with_signal("gh pr create --fill")

    def test_pr_base_dev_allowed(self):
        self._assert_allowed_with_signal("gh pr create --base dev --head feat --fill")

    def test_pr_no_signal_blocked(self):
        from unittest.mock import patch

        from hooks.hook_manager import BlockAction

        with patch("hooks.context.branch_guard._has_pr_signal", return_value=False):
            with pytest.raises(BlockAction):
                self._check("gh pr create --base dev")
