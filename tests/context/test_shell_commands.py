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


@pytest.mark.parametrize(
    "command, expected",
    [
        ("env --split-string 'pytest' -q", [["pytest", "-q"]]),
        ("env -S'pytest' -q", [["pytest", "-q"]]),
        ("env --split-string='FOO=bar pytest' -q", [["pytest", "-q"]]),
        ("env -S", []),
        ("npx --package vitest vitest run", [["vitest", "run"]]),
        ("command -- --flag", [["--flag"]]),
        ("bash -e app.sh", [["bash", "-e", "app.sh"]]),
        ("bash -c", [["bash", "-c"]]),
        ("echo Xpytest", [["echo", "Xpytest"]]),
        ("echo a\tpytest\recho", [["echo", "a", "pytest", "echo"]]),
        ("echo a; bash -c 'pytest -q'", [["echo", "a"], ["pytest", "-q"]]),
        ("echo '$(pytest)'", [["echo", "$(pytest)"]]),
        ("echo '\x60pytest\x60'", [["echo", "\x60pytest\x60"]]),
        (r'echo "\$(pytest)"', [["echo", r"\$(pytest)"]]),
        (r"echo '\$(pytest)'", [["echo", r"\$(pytest)"]]),
        (
            'echo \'literal\' "$(pytest)"; echo "$(jest)"',
            [
                ["echo", "literal", "$(pytest)"],
                ["echo", "$(jest)"],
                ["pytest"],
                ["jest"],
            ],
        ),
        (r'echo "\a$(pytest)"', [["echo", r"\a$(pytest)"], ["pytest"]]),
        ('echo "$(pytest)"', [["echo", "$(pytest)"], ["pytest"]]),
        ("$(pytest)", [["$"], ["pytest"], ["pytest"]]),
    ],
)
def test_parser_preserves_commands_and_only_executes_shell_expansions(command, expected):
    from hooks.context.shell_commands import commands

    assert commands(command) == expected


def test_executable_heredocs_preserve_multiline_programs_and_later_commands():
    from hooks.context.shell_commands import commands

    parsed = commands("echo a\necho b\npython - <<'PY'\nimport pytest\npytest.main()\nPY\necho done")
    assert ["python", "-c", "import pytest\npytest.main()\n"] in parsed
    assert ["echo", "a"] in parsed
    assert ["echo", "b"] in parsed
    assert ["echo", "done"] in parsed
    assert ["cat"] == commands("cat <<'END'\npytest\nEND")[0]
    assert ["pytest"] in commands("bash <<-END\n\tpytest\n\tEND")
    assert ["cat", "<input"] in commands("cat <input")


def test_nesting_boundary_rejects_excess_and_keeps_ten_shells():
    import shlex

    from hooks.context.shell_commands import commands

    assert commands("pytest", 10) == [["pytest"]]
    with pytest.raises(ValueError) as error:
        commands("pytest", 11)
    assert str(error.value) == "Shell wrapper nesting exceeds ten levels"
    nested = "pytest"
    for _ in range(10):
        nested = "bash -c " + shlex.quote(nested)
    assert commands(nested) == [["pytest"]]
    with pytest.raises(ValueError):
        commands("bash -c " + shlex.quote(nested))
    with pytest.raises(ValueError):
        commands("bash -c 'pytest'", 10)
    assert commands("bash -c 'pytest'", 9) == [["pytest"]]
    with pytest.raises(ValueError):
        commands('echo "$(pytest)"', 10)
    assert ["pytest"] in commands('echo "$(pytest)"', 9)
    with pytest.raises(ValueError):
        commands("bash <<EOF\npytest\nEOF", 10)
    assert ["pytest"] in commands("bash <<EOF\npytest\nEOF", 9)


def test_missing_heredoc_delimiter_is_invalid():
    from hooks.context.shell_commands import commands

    with pytest.raises(ValueError) as error:
        commands("bash <<")
    assert str(error.value) == "Missing heredoc delimiter"


@pytest.mark.parametrize(
    "command, expected",
    [
        ("env --split-string 'pytest -q' --maxfail 1", [["pytest", "-q", "--maxfail", "1"]]),
        ("bash <<X\npytest\nX\necho done", [["pytest"], ["bash"], ["echo", "done"]]),
        ('echo "$()"', [["echo", "$()"]]),
        ("echo \"\" '$(pytest)'", [["echo", "", "$(pytest)"]]),
        ("echo " + "'" + "\\" + "'" + ' "$(pytest)"', [["echo", "\\", "$(pytest)"], ["pytest"]]),
        ("bash <<-END\n\tpytest\n\tEND\necho done", [["pytest"], ["bash"], ["echo", "done"]]),
        ("bash <<EOF # << ignored\npytest\nEOF", [["pytest"], ["bash"]]),
        (
            "bash <<EOF\necho ready\npytest -q\nEOF\necho done",
            [
                ["echo", "ready"],
                ["pytest", "-q"],
                ["bash"],
                ["echo", "done"],
            ],
        ),
        (
            "python - <<EOF\n EOF\nEOF \nEOF\necho done",
            [
                ["python", "-c", " EOF\nEOF \n"],
                ["python", "-"],
                ["echo", "done"],
            ],
        ),
    ],
)
def test_remaining_parser_boundaries(command, expected):
    from hooks.context.shell_commands import commands

    assert commands(command) == expected


def test_heredoc_and_separated_shell_depths_share_the_limit():
    from hooks.context.shell_commands import commands

    assert commands("bash -c 'pytest'; echo done", 9) == [["pytest"], ["echo", "done"]]
    assert commands("cat <<EOF\nignored\nEOF", 9) == [["cat"]]
    with pytest.raises(ValueError):
        commands("cat <<EOF\nignored\nEOF", 10)
    script = "bash <<EOF\nbash -c 'pytest'\nEOF"
    assert commands(script, 8) == [["pytest"], ["bash"]]
    with pytest.raises(ValueError):
        commands(script, 9)
