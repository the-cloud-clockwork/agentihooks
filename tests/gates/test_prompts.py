import time
from pathlib import Path

import pytest

from scripts.gates import Call, Gate, Who
from scripts.gates.prompts import (
    PromptGuard,
    inline_scripts,
    refusal,
    removals,
    script_refusal,
    targets,
    variable_programs,
)

ME = Who(name="engineer@1-1", swarm="demo", lane="eng", task="t1")
CWD = "/home/op/dev/worktrees/repo/engineer-1-1"
HOME = "/home/op"
OBSERVED = "mkdir -p /home/op/x && cd /home/op/dev/tcc-ecosystem/agentihooks && rm -rf * ;"
SUBAGENT_OBSERVED = (
    'bash -e -c \'replay() { name=$1; shift; if ! "$@" > /dev/null 2>&1; then echo "failed $name"; fi; }; '
    'replay bad false & failed=0; for job in $(jobs -p); do wait "$job" || failed=1; done; exit "$failed"\'; '
    'echo "exit without return 1: $?"; grep -n "return 1" /home/op/x/swarm_ledger/page_replay.py | head -3'
)
SUBAGENT_RAN = (
    'bash -e -c \'sleep 0.3 & (sleep 0.1; exit 3) & sleep 0.2 & failed=0; echo "listed: $(jobs -p | wc -l)"; '
    'for job in $(jobs -p); do wait "$job" || failed=1; done; echo "failed=$failed"; exit "$failed"\''
)


def bash(command, cwd=CWD):
    return Call("Bash", {"command": command}, cwd=cwd)


def decide(command, who=ME, cwd=CWD):
    return PromptGuard(home=HOME).decide(bash(command, cwd), who, None)


def test_it_is_a_gate_that_ships_enforcing():
    gate = PromptGuard()
    assert isinstance(gate, Gate)
    assert (gate.name, gate.default_mode) == ("prompts", "enforce")


def test_it_matches_bash_calls_naming_rm():
    gate = PromptGuard()
    assert gate.matches(bash("rm -f a.txt"))
    assert gate.matches(bash("rmdir build"))
    assert not gate.matches(bash("ls -la"))
    assert gate.matches(bash("bash -c '\"$@\"' _ ls"))
    assert gate.matches(bash("sh -c '`which ls`'"))
    assert not gate.matches(Call("Read", {"command": "rm -rf *"}))


def test_the_observed_command_is_denied_naming_the_safe_alternatives():
    decision = decide(OBSERVED)
    assert not decision.allowed
    assert "agentihooks scratch rm" in decision.reason
    assert "absolute path" in decision.reason
    assert "'*'" in decision.reason


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf *",
        "rm -f build/*.log",
        "rm -rf /home/op/dev/worktrees/repo/engineer-1-1/dist/?",
        "rmdir /home/op/dev/worktrees/repo/engineer-1-1/[ab]",
    ],
)
def test_a_glob_target_is_denied(command):
    decision = decide(command)
    assert not decision.allowed
    assert "glob" in decision.reason


@pytest.mark.parametrize(
    "command",
    [
        'rm -rf "$TARGET"',
        "rm -rf ${DIR}/build",
        "rm -rf $(git rev-parse --show-toplevel)/dist",
        "rm -rf `pwd`/dist",
        'rm -rf "$(mktemp -d)"',
        'bash -c "rm -rf $X"',
        "sudo rm -rf $HOME/.cache",
        "eval 'rm -rf $X'",
        "rm -rf ~other/dir",
        "rm -rf /home/op/x/$NAME",
        "rm -f a.txt; rm -rf $X",
    ],
)
def test_a_target_known_only_when_it_runs_is_denied(command):
    decision = decide(command)
    assert not decision.allowed
    assert "known only when it runs" in decision.reason


@pytest.mark.parametrize(
    "command",
    [
        "cd /home/op/x; rm -rf build",
        "cd /home/op/x\nrm -f notes.txt",
        "cd /home/op/x || true && rm -rf build",
        "pushd /home/op/x; rmdir empty",
        "cd /home/op/x && ls; rm -rf build",
        "cd /home/op/x && rm -rf build",
        'bash -c "cd /home/op/x; rm -f a.txt"',
    ],
)
def test_a_relative_target_after_a_cd_is_denied(command):
    decision = decide(command)
    assert not decision.allowed
    assert "after a cd" in decision.reason


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf /",
        "rm -rf /usr",
        "rm -rf /tmp/",
        "rm -rf ~",
        "rm -rf ~/",
        "rm -rf /home/op",
        "rm -rf .",
        "rm -rf ..",
        "rm -rf /home/op/dev/worktrees/repo/engineer-1-1",
        "rm -rf /home/op/dev/worktrees",
        "rm -rf '\\\\'",
        "rm -rf ~/.",
        "rm -f > /dev/null /usr",
        "rm -f 2>/dev/null /usr",
        "rm -f -- /usr",
        "rm -rf /home/op/x/build /etc",
    ],
)
def test_a_system_home_or_workspace_directory_is_denied(command):
    decision = decide(command)
    assert not decision.allowed
    assert "protected directory" in decision.reason


def test_more_substitutions_than_the_harness_can_read_are_denied():
    command = "echo " + " ".join("$(true)" for _ in range(65)) + "; rm -f /home/op/x/a.txt"
    decision = decide(command)
    assert not decision.allowed
    assert "65 command substitutions" in decision.reason


def test_substitutions_up_to_the_cap_pass_and_backticks_count():
    assert decide("echo " + " ".join("$(true)" for _ in range(64)) + "; rm -f /home/op/x/a.txt").allowed
    assert not decide("echo " + " ".join("`true`" for _ in range(65)) + "; rm -f /home/op/x/a.txt").allowed
    assert decide("echo " + " ".join("$(true)" for _ in range(65))).allowed


@pytest.mark.parametrize(
    "command",
    [
        "rm -f /home/op/dev/worktrees/repo/engineer-1-1/notes.txt",
        "rm notes.txt",
        "rm -rf build dist",
        "rm -f -- -odd-name",
        "rmdir /home/op/x/empty",
        "rm -f build; cd /home/op/x",
        "cd /home/op/x; rm -rf /home/op/x/build",
        "cd /home/op/x; rm -f ~/notes.txt",
        "rm -rf /home/op/Xdir",
        "rm -rf /srv/data",
        "rm -f /home/op/x/a.txt > /tmp",
        "rm -rf /home/op/x/build 2>&1",
        "rm -f /home/op/x/a.txt 2>/dev/null",
        "rm -f /home/op/x/a.txt > /dev/null",
        "cat <<'EOF' > s.sh\nrm -rf $X *\nEOF",
        "echo rm -rf '*'",
        "git rm -r --cached build",
        "agentihooks scratch rm /home/op/scratchpad/repo/task",
        "rm -rf ../sibling",
    ],
)
def test_a_named_target_passes(command):
    assert decide(command).allowed


def test_the_refusal_names_the_reason_and_both_safe_forms():
    assert refusal("why") == (
        "The harness stops this rm for a yes or no that nobody in a swarm answers: why. Remove a scratch folder with "
        "agentihooks scratch rm <dir>; otherwise name each file or directory by its absolute path, with no glob, "
        "variable or command output, and no cd before it."
    )


def test_each_class_names_its_target():
    assert decide("rm -rf /usr").reason == refusal(
        "the target '/usr' is a protected directory: the filesystem root, a top level directory, the home "
        "directory, or the working directory or one of its parents"
    )
    assert decide("rm -f $X").reason == refusal(
        "the target '$X' is a variable or command output known only when it runs"
    )
    assert decide("rm -f a*").reason == refusal("the target 'a*' is a glob")
    assert decide("cd /a; rm b").reason == refusal(
        "the relative target 'b' runs after a cd that can fail and leave the shell elsewhere"
    )


def test_removals_yield_each_rm_with_whether_a_cd_came_first():
    assert list(removals("rm a; cd /x; command rmdir b; ls; rm -- c")) == [
        (["a"], False),
        (["b"], True),
        (["--", "c"], True),
    ]
    assert list(removals("-x; rm a")) == [(["a"], False)]


def test_targets_skip_options_and_redirects():
    assert targets(["-rf", "a", "2>/dev/null", ">", "out", "--", "-b", "c"]) == ["a", "-b", "c"]


def test_the_home_defaults_to_the_user_home():
    assert PromptGuard().home == str(Path.home())


def test_an_unpinned_session_is_never_judged():
    assert PromptGuard(home=HOME).decide(bash("rm -rf *"), Who(), None).allowed


def test_with_no_working_directory_only_the_dot_paths_stand_for_the_workspace():
    assert not decide("rm -rf ./", cwd="").allowed
    assert not decide("rm -rf ..", cwd="").allowed
    assert not decide("rm -rf /usr", cwd="").allowed
    assert decide("rm -f a.txt", cwd="").allowed


def test_the_observed_subagent_script_is_denied_naming_the_variable_command():
    decision = decide(SUBAGENT_OBSERVED)
    assert not decision.allowed
    assert decision.reason == script_refusal("$@")


def test_a_shell_c_script_with_literal_programs_still_runs():
    assert decide(SUBAGENT_RAN).allowed


@pytest.mark.parametrize(
    "command",
    [
        "bash -c 'f() { \"$@\"; }; f ls'",
        "sh -c '$CMD --version'",
        "bash -ec 'if ! \"$@\"; then exit 1; fi' _ true",
        "bash -lc 'cd /home/op/x && $TOOL run'",
        "zsh -c '`which ls` -la'",
        "sudo bash -c 'while true; do $NEXT; done'",
        "bash -c 'for f in a b; do \"$f\"; done'",
        "ssh-agent; bash -c \"bash -c '\\$RUN'\"",
        "bash -c 'sh <<EOF\n$X\nEOF'",
        "bash -euo pipefail -c '\"$@\"' _ ls",
        "bash -oe pipefail -c '$X'",
        "bash -co pipefail '$X'",
        "bash -O extglob +o posix --rcfile /home/op/x/rc -c '$X'",
        "bash -c 'cat <<EOF | sh\n$X\nEOF'",
    ],
)
def test_a_shell_c_script_running_a_variable_command_is_denied(command):
    decision = decide(command)
    assert not decision.allowed
    assert "shell -c script" in decision.reason


@pytest.mark.parametrize(
    "command",
    [
        "bash -c 'echo \"$@\"' _ a b",
        "bash -c 'for f in a b; do echo $f; done'",
        "bash -c 'X=$(date); echo $X'",
        "$EDITOR notes.txt",
        "echo bash -c '$X'",
        "bash script.sh $X",
        'bash deploy.sh -c "$TARGET"',
        "sh -e build.sh -c $ENV",
    ],
)
def test_a_variable_outside_command_position_or_outside_a_shell_c_script_passes(command):
    assert decide(command).allowed


def test_an_unpinned_session_runs_a_variable_command_script():
    assert PromptGuard(home=HOME).decide(bash(SUBAGENT_OBSERVED), Who(), None).allowed


def test_inline_scripts_read_each_shell_c_form():
    text = "bash x.sh -c 'e'; bash -c 'a' -c 'f'; sh +x -ec 'b'; zsh -o pipefail -c 'c'; ls -c 'd'; bash -c"
    assert list(inline_scripts(text)) == ["a", "b", "c", ""]
    assert list(inline_scripts("bash -o")) == []


def test_inline_scripts_find_no_script_in_a_long_option_word():
    assert list(inline_scripts("bash -" + "c" * 50_000 + "1 x")) == []
    assert list(inline_scripts("bash -" + "o" * 50_000 + "1 x")) == []


@pytest.mark.wall_clock
def test_inline_scripts_read_a_long_option_word_in_linear_time():
    started = time.perf_counter()
    list(inline_scripts("bash -" + "c" * 50_000 + "1 x"))
    list(inline_scripts("bash -" + "o" * 50_000 + "1 x"))
    assert time.perf_counter() - started < 1


def test_variable_programs_skip_keywords_and_assignments():
    assert list(variable_programs('if ! "$@"; then X=1 $Y; fi; { $Z; }; name=$1; echo $W')) == ["$@", "$Y", "$Z"]


def test_the_script_refusal_names_the_command_and_both_safe_forms():
    assert script_refusal("$@") == (
        "The harness stops this shell -c script for a yes or no that nobody in a swarm answers: it runs the command "
        "'$@', a variable or command output the harness reads as an rm it cannot check. Name each program in the "
        "script literally, or run the commands without a shell -c wrapper."
    )
