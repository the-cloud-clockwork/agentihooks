"""Shared command stripping keeps the rest of a heredoc delimiter line for the guards."""

import pytest

from hooks.context._strip import strip_non_command_content
from hooks.context.branch_guard import check_branch_guard
from hooks.context.prod_lockdown import check_prod_lockdown
from hooks.hook_manager import BlockAction

pytestmark = pytest.mark.usefixtures("outside_a_guarded_repository")

OPENERS = ["<<EOF", "<<'EOF'", "<<-EOF"]
BODIES = ["\nbody line\nEOF", "\nbody line"]


def _payload(command: str) -> dict:
    return {"tool_input": {"command": command}, "session_id": "test-heredoc-delimiter-line"}


@pytest.mark.feature("guard-stripping-keeps-heredoc-delimiter-line")
@pytest.mark.parametrize("body", BODIES)
@pytest.mark.parametrize("opener", OPENERS)
def test_stripping_keeps_the_delimiter_line_and_drops_the_body(opener: str, body: str) -> None:
    stripped = strip_non_command_content(f"cat {opener} > out.txt && git status{body}")

    assert "> out.txt && git status" in stripped
    assert "body line" not in stripped


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
@pytest.mark.parametrize("opener", OPENERS)
def test_a_guarded_command_inside_the_body_stays_data(opener: str) -> None:
    check_branch_guard(_payload(f"cat {opener} > notes.md\ngit push origin main\nEOF"))
