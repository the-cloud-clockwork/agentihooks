import pytest


def _check(command):
    from hooks.context.branch_guard import check_branch_guard
    from hooks.context.prod_lockdown import check_prod_lockdown

    payload = {"tool_input": {"command": command}, "session_id": "test-prod-operands"}
    check_prod_lockdown(payload)
    check_branch_guard(payload)


@pytest.mark.parametrize(
    "command",
    [
        'git status && gh issue comment 791 --body "do not run gh pr merge main"',
        "gh issue comment 791 --body 'gh workflow run release.yml is gated'; git status",
        'gh pr merge 123 --base dev && gh issue comment 791 --body "merged into main"',
        'gh pr merge 123 --base dev --subject "merge into main"',
        'gh issue comment 791 --body "docker push ghcr.io/anton/agent:latest is gated"',
        'git commit -m "gh pr merge main"',
    ],
)
def test_quoted_message_is_not_an_operation(command):
    _check(command)


@pytest.mark.parametrize(
    "message", ['--subject "main"', "--subject='master'", '-t "v1"', '--body "main"', "-b 'master'"]
)
def test_single_word_merge_message_is_not_a_branch(message):
    _check(f"gh pr merge 123 --base dev {message}")


@pytest.mark.parametrize("separator", [";", "\n", "&&", "||", "|"])
@pytest.mark.parametrize("commands", [("gh pr", "merge main"), ("gh workflow", "run release.yml")])
def test_operands_of_separate_commands_do_not_join(separator, commands):
    from hooks.context.prod_lockdown import check_prod_lockdown

    check_prod_lockdown({"tool_input": {"command": separator.join(commands)}})


@pytest.mark.parametrize("separator", [";", "\n", "&&", "||", "|"])
@pytest.mark.parametrize(
    "operation", ["git push origin main", "gh pr merge 123 --base main", "gh workflow run release.yml"]
)
def test_real_protected_operation_in_same_call_is_blocked(separator, operation):
    command = f'gh issue comment 791 --body "merge into main"{separator}{operation}'
    from hooks.hook_manager import BlockAction

    with pytest.raises(BlockAction):
        _check(command)


@pytest.mark.parametrize(
    "command",
    [
        'gh pr merge 123 --base "main"',
        "bash -lc 'gh pr merge 123 --base master'",
        'sh -c "gh workflow run release.yml"',
        'gh issue comment 791 --body "merge into main"; docker push ghcr.io/anton/agent:latest',
    ],
)
def test_real_production_operand_is_blocked(command):
    from hooks.hook_manager import BlockAction

    with pytest.raises(BlockAction):
        _check(command)


@pytest.mark.parametrize(
    "command",
    [
        'gh issue comment 791 --body "$(gh pr merge 123 --base main)"',
        'gh issue comment 791 --body "`gh pr merge 123 --base main`"',
        'echo "$(gh workflow run release.yml)"',
        "cat <(gh pr merge 123 --base main)",
        "sh -c 'gh issue comment 791 --body \"$(gh pr merge 123 --base main)\"'",
    ],
)
def test_executed_substitution_is_still_an_operation(command):
    from hooks.hook_manager import BlockAction

    with pytest.raises(BlockAction):
        _check(command)


@pytest.mark.parametrize(
    "command",
    [
        'gh issue comment 791 --body "${x:-$(gh pr merge 123 --base main)}"',
        'gh issue comment 791 --body "${x:-`gh pr merge 123 --base main`}"',
    ],
)
def test_parameter_expansion_keeps_executed_commands(command):
    from hooks.hook_manager import BlockAction

    with pytest.raises(BlockAction):
        _check(command)


def test_arithmetic_does_not_turn_a_literal_message_into_an_operation():
    _check('echo $((1+2)); gh issue comment 791 --body "gh pr merge main"')


@pytest.mark.parametrize(
    "command",
    [
        "gh issue comment 791 --body '$(gh pr merge 123 --base main)'",
        "gh issue comment 791 --body '`gh pr merge 123 --base main`'",
        'gh --repo owner/repo pr merge 123 --base dev --subject "main"',
        'gh pr merge 123 --subject "main" --base dev',
        'gh pr merge 123 --subject="main" --body=master --base dev',
        "git status # gh pr merge main",
        "cat <<'EOF'\ngh pr merge main\nEOF\ngit status",
    ],
)
def test_inert_message_text_passes(command):
    _check(command)


@pytest.mark.parametrize(
    "command",
    [
        'gh pr merge 123 --subject "main" --base master',
        'gh pr merge 123 --subject "main" --base=main',
        'gh pr merge main --subject "dev"',
        'echo "$(echo "$(gh pr merge 123 --base main)")"',
        "sudo bash -lc 'echo \"$(gh workflow run release.yml)\"'",
        'gh pr merge main "unterminated',
        "echo $((1+2)); gh pr merge 123 --base main",
        "cat <<'EOF'\nmessage\nEOF\ngh pr merge 123 --base main",
    ],
)
def test_message_filter_and_parser_fallback_preserve_real_operations(command):
    from hooks.hook_manager import BlockAction

    with pytest.raises(BlockAction):
        _check(command)


def test_allowed_controls_operation_cannot_skip_a_protected_merge():
    from hooks.context.controls_toggle import set_controls_disabled

    set_controls_disabled("test-prod-operands")
    from hooks.hook_manager import BlockAction

    with pytest.raises(BlockAction, match="gh pr merge to main/master/v1"):
        _check("gh workflow run release.yml; gh pr merge 123 --base main")
