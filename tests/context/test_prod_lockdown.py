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


def test_merge_message_may_contain_several_equals():
    _check('gh pr merge 123 --base dev --subject="main=checkpoint=done"')


def test_inline_message_cannot_hide_the_following_real_target():
    from hooks.hook_manager import BlockAction

    with pytest.raises(BlockAction):
        _check('gh pr merge 123 --subject="message" --base=main')


def test_shell_script_message_is_not_executed():
    _check('sh notify.sh "gh pr merge 123 --base main"')


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


def test_parameter_default_hash_does_not_comment_out_a_substitution():
    from hooks.hook_manager import BlockAction

    with pytest.raises(BlockAction):
        _check('echo "${x:-#`gh pr merge 123 --base main`}"')


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


_SIGNAL_SESSION = "test-prod-operands"
_TAG_PUSH = "docker push ghcr.io/anton/agent:latest"
_MAIN_MERGE = "gh pr merge 123 --base main"


@pytest.fixture
def grant(tmp_path, monkeypatch):
    from hooks.context import prod_lockdown
    from hooks.context.controls_toggle import set_controls_disabled

    monkeypatch.setattr(prod_lockdown, "get_redis", lambda: None)
    monkeypatch.setattr(prod_lockdown, "AGENTIHOOKS_HOME", tmp_path)
    setters = {
        "release": prod_lockdown.set_release_signal,
        "hotfix": prod_lockdown.set_hotfix_signal,
        "controls": set_controls_disabled,
    }
    return lambda signal: setters[signal](_SIGNAL_SESSION)


@pytest.mark.parametrize(
    ("signal", "command"),
    [
        ("release", "gh workflow run release.yml --ref dev"),
        ("release", _MAIN_MERGE),
        ("hotfix", _TAG_PUSH),
        ("hotfix", _MAIN_MERGE),
        ("controls", _TAG_PUSH),
        ("controls", "gh workflow run release.yml --ref dev"),
    ],
)
def test_signal_authorizes_its_own_operation(grant, signal, command):
    grant(signal)
    _check(command)


@pytest.mark.parametrize("separator", [";", "\n", "&&", "||", "|"])
@pytest.mark.parametrize(
    ("signal", "authorized", "refused", "block"),
    [
        ("release", "gh workflow run release.yml --ref dev", _TAG_PUSH, "BLOCKED [image tag"),
        ("release", _MAIN_MERGE, _TAG_PUSH, "BLOCKED [image tag"),
        ("release", _MAIN_MERGE, "git push origin main", "BLOCKED: Pushing directly to main/master"),
        ("hotfix", _TAG_PUSH, "git push origin main", "BLOCKED: Pushing directly to main/master"),
        ("hotfix", _MAIN_MERGE, "git push origin main", "BLOCKED: Pushing directly to main/master"),
        ("controls", _TAG_PUSH, _MAIN_MERGE, "BLOCKED [gh pr merge to main/master/v1]"),
        ("controls", "gh workflow run release.yml --ref dev", _MAIN_MERGE, "BLOCKED [gh pr merge to main/master/v1]"),
    ],
)
@pytest.mark.parametrize("refused_first", [False, True])
def test_signal_does_not_authorize_another_command_in_the_chain(
    grant, separator, signal, authorized, refused, block, refused_first
):
    from hooks.hook_manager import BlockAction

    grant(signal)
    commands = [refused, authorized] if refused_first else [authorized, refused]
    with pytest.raises(BlockAction) as blocked:
        _check(separator.join(commands))
    assert str(blocked.value).startswith(block)
