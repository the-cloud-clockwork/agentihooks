import pytest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "command",
    [
        "git push origin HEAD",
        "timeout -s TERM -k 5s 30s git push origin HEAD",
        "sudo -u worker nice -n 5 env -u UNUSED FOO=bar git push origin HEAD",
        "bash -lc 'git push origin HEAD'",
        "FOO=bar command -- git push origin HEAD",
    ],
)
def test_wrappers_preserve_the_executable_and_arguments(command):
    from hooks.context.shell_commands import commands

    assert ["git", "push", "origin", "HEAD"] in commands(command)


def test_quoted_arguments_and_separate_commands_are_preserved():
    from hooks.context.shell_commands import commands

    assert commands("git -C '/a path' push origin HEAD && echo 'pytest -q'") == [
        ["git", "-C", "/a path", "push", "origin", "HEAD"],
        ["echo", "pytest -q"],
    ]
