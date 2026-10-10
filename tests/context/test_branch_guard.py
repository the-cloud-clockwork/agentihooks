"""Tests for hooks.context.branch_guard."""

import pytest

pytestmark = pytest.mark.usefixtures("outside_a_guarded_repository")


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


class TestOnlyTheCommandsOwnOperands:
    """A protected word in other text of the call never blocks; an operand of the git command does."""

    def _check(self, command: str):
        from hooks.context.branch_guard import check_branch_guard

        check_branch_guard({"tool_input": {"command": command}, "session_id": "test-operands"})

    @pytest.mark.parametrize(
        "command",
        [
            'git branch -D engineer-1 && gh issue comment 791 --body "Merged; master untouched"',
            'gh issue comment 791 --body "do not run git branch -D master" && git branch -d old',
            'git push origin feat && gh issue comment 5 --body "git push origin main is blocked"',
            'gh issue comment 5 --body "run git checkout -b topic, or git switch -c topic"',
            "gh issue comment 5 --body 'name it with git branch topic'",
            "git branch -D old && echo master",
            "git branch -D old;echo master",
            'grep -c "git push origin main" hooks.log',
            'sh notify.sh "git push origin main went through"',
            'git branch --format="%(refname:short) %(upstream:short)"',
        ],
    )
    def test_protected_word_in_other_text_allowed(self, command):
        self._check(command)

    @pytest.mark.parametrize(
        "command",
        [
            'git checkout -b topic && gh issue comment 5 --body "then git push origin main"',
            "git branch topic && gh issue comment 5 --body 'then git merge master'",
        ],
    )
    def test_branch_create_with_protected_word_in_a_comment_allowed(self, command):
        from unittest.mock import patch

        with patch("hooks.context.branch_guard._has_branch_signal", return_value=True):
            self._check(command)

    @pytest.mark.parametrize(
        "command",
        [
            'git push origin "main"',
            "git push origin 'HEAD:main'",
            "git branch -D 'master'",
            "git push origin main; echo pushed",
            'bash -c "git push origin main"',
            "bash -lc 'git branch -D master'",
            'sudo sh -c "git push origin HEAD:master"',
            "curl https://example.com/X#top; git push origin main",
            'git branch -D main "unterminated',
        ],
    )
    def test_operand_of_the_git_command_blocked(self, command):
        from hooks.hook_manager import BlockAction

        with pytest.raises(BlockAction):
            self._check(command)


class TestPRBaseGuard:
    """gh pr create must name an explicit --base.

    A dev→main snapshot PR is the sanctioned way into main (CI Manifesto §4/§5),
    so --base main is allowed under the PR signal. Only a bare create is blocked,
    because it targets the default branch implicitly."""

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

    def test_pr_base_main_allowed(self):
        # The dev→main snapshot PR is the only sanctioned path into main.
        self._assert_allowed_with_signal("gh pr create --base main --fill")

    def test_pr_base_master_allowed(self):
        self._assert_allowed_with_signal("gh pr create --base master -t x")

    def test_pr_bare_create_blocked(self):
        # a bare create defaults to the repo default branch (main)
        self._assert_blocked_with_signal("gh pr create --fill")

    def test_pr_base_dev_allowed(self):
        self._assert_allowed_with_signal("gh pr create --base dev --head feat --fill")

    @pytest.mark.parametrize("base", ["main", "master", "v1", "feat-x"])
    def test_pr_no_signal_blocked(self, base):
        from unittest.mock import patch

        from hooks.hook_manager import BlockAction

        with (
            patch("hooks.context.branch_guard._has_pr_signal", return_value=False),
            patch("hooks.context.controls_toggle.is_controls_disabled", return_value=False),
        ):
            with pytest.raises(BlockAction):
                self._check(f"gh pr create --base {base} --fill")

    def test_pr_into_dev_needs_no_signal_or_counter(self):
        from unittest.mock import patch

        with (
            patch("hooks.context.branch_guard._has_pr_signal", return_value=False),
            patch("hooks.context.branch_guard._get_pr_counter", return_value=99),
            patch("hooks.context.controls_toggle.is_controls_disabled", return_value=False),
        ):
            self._check("gh pr create --base dev --fill")
            self._check("gh pr create --base=dev --fill")

    def test_pr_base_read_from_the_create_command_only(self):
        from unittest.mock import patch

        with (
            patch("hooks.context.branch_guard._has_pr_signal", return_value=False),
            patch("hooks.context.controls_toggle.is_controls_disabled", return_value=False),
        ):
            self._check('gh pr create --base dev --body "never use --base main"')
            self._check('gh issue comment 5 --body "gh pr create --base main needs a signal"')


class TestPrSignalResetsCounter:
    """Re-signaling PR authorization must reset the per-session PR counter
    (CI Manifesto §8: max 3 per session, then re-signal). The limit-reached
    error message promises this reset — set_pr_signal must deliver it."""

    def test_set_pr_signal_clears_counter(self, tmp_path):
        from unittest.mock import patch

        from hooks.context import branch_guard

        with (
            patch("hooks.context.branch_guard.get_redis", return_value=None),
            patch("hooks.context.branch_guard.AGENTIHOOKS_HOME", tmp_path),
        ):
            sid = "test-pr-reset"
            # Exhaust the counter the same way real PR creations do
            for _ in range(3):
                branch_guard.increment_pr_counter(sid)
            assert branch_guard._get_pr_counter(sid) == 3
            # Operator re-signal must reset it
            branch_guard.set_pr_signal(sid)
            assert branch_guard._get_pr_counter(sid) == 0


class TestResolveCwd:
    @pytest.mark.parametrize("quote", ['"', "'", ""])
    def test_a_cd_prefix_names_the_directory(self, tmp_path, quote):
        from hooks.context.branch_guard import _resolve_cwd

        work = tmp_path / "workX"
        work.mkdir()

        assert _resolve_cwd(f"cd {quote}{work}{quote} && git push", "/") == str(work)

    def test_a_cd_prefix_expands_home_and_variables(self, tmp_path, monkeypatch):
        from hooks.context.branch_guard import _resolve_cwd

        (tmp_path / "work").mkdir()
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("WORK_DIR", str(tmp_path / "work"))

        assert _resolve_cwd("cd ~/work && git push", "/") == str(tmp_path / "work")
        assert _resolve_cwd("cd $WORK_DIR && git push", "/") == str(tmp_path / "work")

    def test_a_missing_payload_folder_falls_back_to_the_process_folder(self, tmp_path, monkeypatch):
        from hooks.context.branch_guard import _resolve_cwd

        monkeypatch.chdir(tmp_path)

        assert _resolve_cwd("git commit -m x", str(tmp_path / "missing")) == str(tmp_path)


def test_a_push_of_a_head_that_skipped_the_cheap_gates_is_blocked(tmp_path):
    import subprocess

    from hooks.context.branch_guard import check_branch_guard
    from hooks.hook_manager import BlockAction

    (tmp_path / "scripts" / "ci_prepush").mkdir(parents=True)
    (tmp_path / "scripts" / "ci_prepush" / "__init__.py").write_text("")
    for args in (
        ["init", "-q"],
        ["-c", "user.email=t@x", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "b"],
    ):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True)

    with pytest.raises(BlockAction, match="python -m scripts.ci_prepush"):
        check_branch_guard({"tool_input": {"command": "git push origin HEAD"}, "cwd": str(tmp_path)})
