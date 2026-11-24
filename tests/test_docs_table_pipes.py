"""Docs table code never escapes a double pipe, because kramdown prints the backslash inside a code span."""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

DOCS = Path(__file__).resolve().parents[1] / "docs"
CODE = re.compile(r"(`+)(?P<span>.+?)\1|<code>(?P<element>.+?)</code>")


def escaped_double_pipe_lines(text: str) -> list[int]:
    """Return the line numbers of table rows whose code span or code element holds an escaped double pipe."""
    return [
        number
        for number, line in enumerate(text.splitlines(), 1)
        if line.lstrip().startswith("|")
        and any("\\|\\|" in (code["span"] or code["element"]) for code in CODE.finditer(line))
    ]


@pytest.mark.feature("docs-table-double-pipe")
def test_the_check_flags_an_escaped_double_pipe_and_passes_the_entity_form() -> None:
    page = "\n".join(
        [
            "| Job | Expression |",
            "|---|---|",
            "| `a` | `${{ x \\|\\| y }}` |",
            "| `b` | <code>${{ x &#124;&#124; y }}</code> |",
            "| `c` | <code>${{ x \\|\\| y }}</code> |",
            "| `d` | `a \\| b` |",
            "Prose keeps `x \\|\\| y` out of any table.",
        ]
    )
    assert escaped_double_pipe_lines(page) == [3, 5]


@pytest.mark.feature("docs-table-double-pipe")
def test_no_docs_table_code_span_escapes_a_double_pipe() -> None:
    hits = [
        f"{path.relative_to(DOCS.parent)}:{number}"
        for path in sorted(DOCS.rglob("*.md"))
        for number in escaped_double_pipe_lines(path.read_text(encoding="utf-8"))
    ]
    assert hits == []
