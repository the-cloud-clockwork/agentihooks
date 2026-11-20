"""Docs table code never escapes a double pipe, because kramdown prints the backslash inside a code span."""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

DOCS = Path(__file__).resolve().parents[1] / "docs"
CODE_SPAN = re.compile(r"(`+)(.+?)\1")


def escaped_double_pipe_lines(text: str) -> list[int]:
    """Return the line numbers of table rows whose code span holds an escaped double pipe."""
    return [
        number
        for number, line in enumerate(text.splitlines(), 1)
        if line.lstrip().startswith("|") and any("\\|\\|" in span.group(2) for span in CODE_SPAN.finditer(line))
    ]


@pytest.mark.feature("docs-table-double-pipe")
def test_the_check_flags_an_escaped_double_pipe_and_passes_the_entity_form() -> None:
    page = "\n".join(
        [
            "| Job | Expression |",
            "|---|---|",
            "| `a` | `${{ x \\|\\| y }}` |",
            "| `b` | <code>${{ x &#124;&#124; y }}</code> |",
            "| `c` | `a \\| b` |",
            "Prose keeps `x \\|\\| y` out of any table.",
        ]
    )
    assert escaped_double_pipe_lines(page) == [3]


@pytest.mark.feature("docs-table-double-pipe")
def test_no_docs_table_code_span_escapes_a_double_pipe() -> None:
    hits = [
        f"{path.relative_to(DOCS.parent)}:{number}"
        for path in sorted(DOCS.rglob("*.md"))
        for number in escaped_double_pipe_lines(path.read_text())
    ]
    assert hits == []
