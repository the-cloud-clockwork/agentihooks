"""Shared command stripping keeps the rest of a heredoc delimiter line for the guards."""

from collections.abc import Callable

import pytest

from hooks.context._strip import strip_non_command_content
from hooks.context.branch_guard import check_branch_guard
from hooks.context.prod_lockdown import check_prod_lockdown
from hooks.hook_manager import BlockAction

pytestmark = pytest.mark.usefixtures("outside_a_guarded_repository")

OPENERS = ["<<EOF", "<<'EOF'", "<<-EOF"]
TERMINATED = ["\nbody line\nEOF", "\n\tbody line\n\tEOF"]
BODIES = [*TERMINATED, "\nbody line"]


def _payload(command: str) -> dict:
    return {"tool_input": {"command": command}, "session_id": "test-heredoc-delimiter-line"}


@pytest.mark.feature("guard-stripping-keeps-heredoc-delimiter-line")
@pytest.mark.parametrize("body", TERMINATED)
@pytest.mark.parametrize("opener", OPENERS)
def test_stripping_keeps_the_delimiter_line_and_drops_the_body(opener: str, body: str) -> None:
    assert strip_non_command_content(f"cat {opener} > out.txt && git status{body}") == "cat  > out.txt && git status"


@pytest.mark.feature("guard-stripping-keeps-heredoc-delimiter-line")
def test_stripping_blanks_a_shift_inside_arithmetic() -> None:
    assert strip_non_command_content("echo $(( 1 << x ))\nx") == "echo $(( 1    x ))\nx"


@pytest.mark.feature("guard-stripping-keeps-heredoc-delimiter-line")
@pytest.mark.parametrize("body", BODIES)
@pytest.mark.parametrize("opener", OPENERS)
def test_branch_guard_refuses_a_push_to_main_after_the_delimiter(opener: str, body: str) -> None:
    with pytest.raises(BlockAction):
        check_branch_guard(_payload(f"cat {opener} > out.txt && git push origin main{body}"))


@pytest.mark.feature("guard-stripping-keeps-heredoc-delimiter-line")
@pytest.mark.parametrize("body", BODIES)
@pytest.mark.parametrize("opener", OPENERS)
def test_prod_lockdown_refuses_a_release_run_after_the_delimiter(opener: str, body: str) -> None:
    with pytest.raises(BlockAction):
        check_prod_lockdown(_payload(f"cat {opener} > out.txt && gh workflow run release.yml{body}"))


@pytest.mark.feature("guard-stripping-keeps-heredoc-delimiter-line")
@pytest.mark.parametrize(
    "command",
    [
        "cat <<-EOF > out.txt\n\tbody line\n\tEOF\ngit push origin main",
        "echo $((1 << 2))\ngit push origin main",
        "echo $((1<<git))\ngit push origin main\ngit status",
        "echo $((1 << x))\ngit push origin main\nx",
        "echo $(( 1 << x ))\ngit push origin main\nx",
        "echo $((1 << x; 2))\ngit push origin main\nx",
        "echo $((\n1 << x\n))\ngit push origin main\nx",
        "cat <<EOF > out.txt\ngit push origin main",
    ],
)
def test_branch_guard_refuses_a_push_on_a_line_after_an_opener(command: str) -> None:
    with pytest.raises(BlockAction):
        check_branch_guard(_payload(command))


@pytest.mark.feature("guard-stripping-keeps-heredoc-delimiter-line")
@pytest.mark.parametrize("opener", OPENERS)
@pytest.mark.parametrize(
    ("guard", "operation"),
    [(check_branch_guard, "git push origin main"), (check_prod_lockdown, "gh workflow run release.yml")],
)
def test_a_guarded_command_inside_the_body_stays_data(
    guard: Callable[[dict], None], operation: str, opener: str
) -> None:
    guard(_payload(f"cat {opener} > notes.md\n{operation}\nEOF"))


@pytest.mark.feature("guard-stripping-keeps-heredoc-delimiter-line")
def test_a_body_closed_with_its_command_substitution_stays_data() -> None:
    check_branch_guard(_payload("notes=$(cat <<EOF\ngit push origin main\nEOF)"))


SHIFTS_OUTSIDE_DOUBLE_PARENTHESES = [
    "echo $(( (1 + (2 << 3)) ))\ngit push origin main\n3",
    "(( x = (1 + (y << n)) ))\ngit push origin main\nn",
    "echo $[ 1 << END ]\ngit push origin main\nEND",
    "a[1<<WORD]=x\ngit push origin main\nWORD",
    "echo ${v:1<<N}\ngit push origin main\nN",
]


@pytest.mark.feature("guard-stripping-reads-heredocs-from-the-bash-parse")
@pytest.mark.parametrize("command", SHIFTS_OUTSIDE_DOUBLE_PARENTHESES)
def test_stripping_keeps_the_line_after_any_arithmetic_shift(command: str) -> None:
    assert "\ngit push origin main\n" in strip_non_command_content(command)


@pytest.mark.feature("guard-stripping-reads-heredocs-from-the-bash-parse")
@pytest.mark.parametrize("command", SHIFTS_OUTSIDE_DOUBLE_PARENTHESES)
def test_branch_guard_refuses_a_push_after_any_arithmetic_shift(command: str) -> None:
    with pytest.raises(BlockAction, match="directly to main"):
        check_branch_guard(_payload(command))


@pytest.mark.feature("guard-stripping-reads-heredocs-from-the-bash-parse")
def test_stripping_drops_a_double_quoted_heredoc_like_a_single_quoted_one() -> None:
    command = 'cat <<"EOF" > out.txt && git status\nbody line\nEOF'
    assert strip_non_command_content(command) == "cat  > out.txt && git status"


@pytest.mark.feature("guard-stripping-reads-heredocs-from-the-bash-parse")
@pytest.mark.parametrize(
    ("command", "stripped"),
    [
        (
            "echo «déjà» && cat <<EOF > notes.md\nnaïve — body\nEOF\ngit status ✓",
            "echo «déjà» && cat  > notes.md\ngit status ✓",
        ),
        ("cat <<EOF > empty.txt\nEOF\ngit status", "cat  > empty.txt\ngit status"),
        (
            "cat <<A > a.txt\nalpha\nA\ngit status\ncat <<'B' > b.txt\nbeta\nB",
            "cat  > a.txt\ngit status\ncat  > b.txt",
        ),
        (
            "cat <<OUTER > notes.md\n$(cat <<INNER\ninner\nINNER\n)\nOUTER\ngit status",
            "cat  > notes.md\ngit status",
        ),
        ("cat <<EOF > notes.md\n\ud800\nEOF\ngit status", "cat  > notes.md\ngit status"),
    ],
)
def test_stripping_keeps_exact_text_around_heredocs(command: str, stripped: str) -> None:
    assert strip_non_command_content(command) == stripped
