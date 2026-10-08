from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def test_capacity_explanation_literal_keeps_source_offsets():
    from hooks.filters.finders.string_literals import find

    source = Path("scripts/swarm/capacity.py").read_text()
    matches = find(source, "scripts/swarm/capacity.py", "Write")
    literal = next(item for item in matches if item["text"].startswith("; Claude has "))
    assert (
        literal["text"] == "; Claude has {placeable['claude']} free seats and Codex has {placeable['codex']} free seats"
    )
    assert source[literal["start"] : literal["end"]] == literal["text"]
    assert literal["reason"] == "user-facing literal"


@pytest.mark.parametrize("extension", ["js", "ts", "jsx", "tsx", "vue", "svelte", "html"])
def test_web_literals_and_text_nodes_keep_offsets(extension):
    from hooks.filters.finders.string_literals import find

    source = 'const name = "hello"; const message = `welcome ${name}`; <p>changed 9m ago</p>'
    findings = find(source, f"page.{extension}", "Write")
    assert [item["text"] for item in findings] == ["hello", "welcome ${name}", "changed 9m ago"]
    assert all(source[item["start"] : item["end"]] == item["text"] for item in findings)
    assert all(item["reason"] == "user-facing literal" for item in findings)


@pytest.mark.parametrize(
    "clause",
    [
        "because accounts have quota",
        "since accounts have quota",
        "; accounts have quota",
        "which means accounts have quota",
        "so that accounts have quota",
    ],
)
def test_explanation_tail_only_flags_clauses_inside_literals(clause):
    from hooks.filters.finders.explanation_tail import find

    source = f'label = "changed 9m ago {clause}"\nstatus = "changed 9m ago"\n# because comment'
    findings = find(source, "page.py", "Write")
    assert len(findings) == 1
    assert findings[0]["text"] == clause
    assert source[findings[0]["start"] : findings[0]["end"]] == clause


def test_explanation_tail_has_the_finder_contract_reason():
    from hooks.filters.finders.explanation_tail import find

    assert find('"hello because quota"', "page.py", "Write") == [
        {"start": 7, "end": 20, "text": "because quota", "reason": "explanation tail"}
    ]


def test_python_multiline_escaped_raw_and_nested_fstrings_keep_source_text():
    from hooks.filters.finders.string_literals import find

    source = "name = r'hello\\nworld'\nlabel = '''first\nsecond'''\nmessage = f\"hello {f'{name}'}\""
    findings = find(source, "page.py", "Edit")
    assert [item["text"] for item in findings] == ["hello\\nworld", "first\nsecond", "hello {f'{name}'}"]
    assert all(source[item["start"] : item["end"]] == item["text"] for item in findings)


@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\v", "\f", "\r"])
def test_python_literal_offsets_count_only_newline_as_a_source_line(separator):
    from hooks.filters.finders.string_literals import find

    source = f"label = 'first{separator}second'\nother = 'because quota'\n"
    findings = find(source, "page.py", "Write")
    assert [item["text"] for item in findings] == [f"first{separator}second", "because quota"]
    assert all(source[item["start"] : item["end"]] == item["text"] for item in findings)


def test_unknown_language_has_no_literals():
    from hooks.filters.finders.string_literals import find

    assert find('"hello"', "file.txt", "Write") == []


def test_unfinished_python_still_returns_complete_literals():
    from hooks.filters.finders.string_literals import find

    assert [item["text"] for item in find('name = "hello"\nx = (', "file.py", "Write")] == ["hello"]


def test_web_escaped_quotes_empty_literals_and_blank_nodes():
    from hooks.filters.finders.string_literals import find

    source = 'const a = "say \\"hello\\""; const b = ""; <p> </p><p>hello</p>'
    assert [item["text"] for item in find(source, "page.tsx", "Write")] == ['say \\"hello\\"', "hello"]
