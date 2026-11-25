"""A Handoff v2 document ends on the handoff marker, whatever other documents the check learns to end."""

import pytest

from scripts.handoff.check import problems, section

pytestmark = pytest.mark.unit

BODY = (
    "# Handoff v2\n"
    "## Intent\n- Refuse a malformed handoff\n"
    "## Done\nNone\n"
    "## Stopped at\n- Seam two, nothing proven\n"
    "## Decisions and promises\n- Keep the cache out\n"
    "## Next\n- Run it\n"
    "## Read first\nNone\n"
)
MARKER = "<!-- handoff complete -->"
UNMARKED = f"the completion marker {MARKER} must be the last line"


def test_a_handoff_v2_that_ends_on_the_handoff_marker_conforms() -> None:
    assert problems(f"{BODY}{MARKER}\n", lambda address: False) == []
    assert problems(f"{BODY}{MARKER}\n\n  \n", lambda address: False) == []


@pytest.mark.parametrize("ending", ["", "<!-- handoff done -->\n", f"{MARKER}\n- one more line\n"])
def test_a_handoff_v2_that_ends_on_anything_else_is_told_its_marker(ending: str) -> None:
    assert UNMARKED in problems(BODY + ending, lambda address: False)


def test_the_marker_is_no_part_of_the_last_section() -> None:
    assert section(f"{BODY}{MARKER}\n", "Read first") == "None"
    assert section(f"{BODY}{MARKER}\n", "Next") == "- Run it"
