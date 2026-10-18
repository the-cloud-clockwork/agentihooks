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
        ("grep -n '<<' f", [["grep", "-n", "<<", "f"]]),
        ('grep -n "cat <<EOF" f', [["grep", "-n", "cat <<EOF", "f"]]),
        ("grep -c \\<\\< f", [["grep", "-c", "<<", "f"]]),
        ("echo 'a\n<<b'\nbash <<EOF\npytest\nEOF", [["pytest"], ["echo", "a\n<<b"], ["bash"]]),
        ("echo ok # don't\nbash <<EOF\npytest\nEOF", [["pytest"], ["echo", "ok"], ["bash"]]),
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


@pytest.mark.parametrize(
    "command, folders",
    [
        ("git push", []),
        ("cd /a && git push", ["/a"]),
        ("(cd /a; git push)", ["/a"]),
        ("(cd /a); git push", []),
        ("(cd /a && true) && git push", []),
        ("cd /a | true; git push", []),
        ("true | cd /a\ngit push", []),
        ("cd /a | git push", []),
        ("cd /a & git push", []),
        ("cd /a && cd /b & git push", []),
        ("{ cd /a; }; git push", ["/a"]),
        ("{ cd /a; } | true; git push", []),
        ("bash -c 'cd /a'; git push", []),
        ("env -C /a git push", ["/a"]),
        ("env --chdir=/a true; git push", []),
        ("env --chdir=/a git push", ["/a"]),
        ("env -C/a git push", ["/a"]),
        ("env -C /a -C /b git push", ["/a", "/b"]),
        ("env --chdir=/a=b git push", ["/a=b"]),
        ("env -u X -C /a git push", ["/a"]),
        ("cd /a && bash -c 'git push'", ["/a"]),
        ("cd /a <&0 && git push", ["/a"]),
        ("cd /a |& true; git push", []),
        ("true | (cd /a && git push)", ["/a"]),
        ("true |(cd /a; git push)", ["/a"]),
        ("cd /a & true >x; git push", []),
        ("cd /a >x & git push", []),
        ("true & cd /a; git push", ["/a"]),
        ("true & true | git push", []),
        ("cd /a || cd /b & git push", []),
        ("env -C /a env -C /b git push", ["/a", "/b"]),
        ("cd /b && env --chdir /a git push", ["/b", "/a"]),
        ("cd /a && cd /b && cd - && git push", ["/a", "/b", "-"]),
        ("cd -P /a && git push", ["/a"]),
        ("cd && git push", ["~"]),
        ("cd /a 2>&1 && git push", ["/a"]),
        ("cd /a &>/dev/null && git push", ["/a"]),
        ("cd /a >&2 && git push", ["/a"]),
        ("bash -c 'cd /a && git push'", ["/a"]),
        ("cd /b; bash <<< 'git push'", ["/b"]),
    ],
)
def test_folder_changes_last_only_inside_their_own_shell(command, folders):
    from hooks.context.shell_commands import located

    assert located(command)[-1] == (["git", "push"], folders)


@pytest.mark.parametrize(
    "command",
    [
        "bash <<< 'pytest -q'",
        "bash <<<'pytest -q'",
        'sudo bash <<< "echo a; pytest -q"',
        "cat <<'EOF' | bash\npytest -q\nEOF",
        "cat <<EOF | sudo sh -s\npytest -q\nEOF\necho done",
    ],
)
def test_scripts_fed_to_a_shell_on_stdin_are_parsed(command):
    from hooks.context.shell_commands import commands

    assert ["pytest", "-q"] in commands(command)


def test_piped_heredoc_keeps_the_rest_of_its_line():
    from hooks.context.shell_commands import commands

    assert commands("cat <<EOF | grep x && echo done\npytest\nEOF") == [
        ["cat"],
        ["grep", "x"],
        ["echo", "done"],
    ]


@pytest.mark.parametrize(
    "command",
    [
        "echo $((1 << 2)); pytest -q",
        "(( x <<= 1 ))\npytest -q",
        "echo $(( 1<<2 )) && pytest -q",
    ],
)
def test_an_arithmetic_left_shift_is_not_a_heredoc(command):
    from hooks.context.shell_commands import commands

    assert ["pytest", "-q"] in commands(command)


@pytest.mark.parametrize(
    "command",
    [
        "cd /b && bash <<EOF\ngit push\nEOF",
        "cd /b && cat <<EOF | bash\ngit push\nEOF",
        "cd /b && bash -c 'bash <<EOF\ngit push\nEOF\necho'",
    ],
)
def test_a_heredoc_script_runs_in_the_folder_of_its_shell(command):
    from hooks.context.shell_commands import located

    assert (["git", "push"], ["/b"]) in located(command)


def test_quoted_parentheses_do_not_hide_a_heredoc():
    from hooks.context.shell_commands import commands

    parsed = commands('echo "((" ; bash <<EOF\npytest -q\nEOF')
    assert ["pytest", "-q"] in parsed
    assert ["bash"] in parsed


@pytest.mark.parametrize(
    "command",
    [
        "bash <<<'<<x'; echo done",
        'x=$((1))"<<"; echo done',
        "echo done &",
        "bash <<'E F'\necho ok\nE F\necho done",
        'bash <<"E F"\necho ok\nE F\necho done',
    ],
)
def test_markers_and_quotes_leave_later_commands_intact(command):
    from hooks.context.shell_commands import commands

    assert ["echo", "done"] in commands(command)


def test_a_substitution_inside_a_nested_shell_keeps_the_outer_folder():
    from hooks.context.shell_commands import located

    pushes = [
        folders for tokens, folders in located("cd /b && bash -c 'echo $(git push)'") if tokens == ["git", "push"]
    ]
    assert pushes and all(folders == ["/b"] for folders in pushes)


def test_stdin_scripts_share_the_nesting_limit():
    import shlex

    from hooks.context.shell_commands import commands, located

    assert commands("bash <<< pytest", 9) == [["pytest"]]
    with pytest.raises(ValueError):
        commands("cat <<EOF | bash -c 'echo'\nx\nEOF", 9)
    nested = "pytest"
    for _ in range(10):
        nested = "bash -c " + shlex.quote(nested)
    assert located(nested) == [(["pytest"], [])]


def test_stdin_script_boundaries():
    from hooks.context.shell_commands import commands, located

    assert commands("bash <<< x y") == [["x"]]
    assert commands("bash <<<") == []
    with pytest.raises(ValueError):
        commands("bash <<< pytest", 10)
    assert ["cat"] in commands("cat <<EOF | bash -c 'echo'\nx\nEOF", 8)
    assert commands("env -i") == []
    assert commands("echo $((1)); cat <<EOF\nx\nEOF") == [["echo", "$"], ["1"], ["cat"]]
    assert located("env -- -C /a")[-1] == (["-C", "/a"], [])
    for command in (
        "echo $((1)); bash <<EOF\npytest -q\nEOF",
        "bash << EOF\npytest -q\nEOF",
        "bash <<EOF | cat\npytest -q\nEOF",
    ):
        assert ["pytest", "-q"] in commands(command)
