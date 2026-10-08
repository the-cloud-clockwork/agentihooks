import pytest

from hooks.filters import extract
from hooks.filters.extract import Piece

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "tool, tool_input, expected",
    [
        ("Write", {"file_path": "a", "content": "body"}, [Piece(("content",), "body")]),
        ("Edit", {"old_string": "x", "new_string": "y"}, [Piece(("new_string",), "y")]),
        ("NotebookEdit", {"new_source": "cell"}, [Piece(("new_source",), "cell")]),
        ("Write", {"content": 3}, []),
        (
            "MultiEdit",
            {"edits": [{"new_string": "one"}, {"old_string": "z"}, "bad", {"new_string": "two"}]},
            [Piece(("edits", 0, "new_string"), "one"), Piece(("edits", 3, "new_string"), "two")],
        ),
        ("MultiEdit", {"edits": "no"}, []),
        ("mcp__ledger__say", {"text": "hello", "evidence": ["x"]}, [Piece(("text",), "hello")]),
        ("mcp__ledger__done", {"evidence": ["a", 2, "b"]}, [Piece(("evidence", 0), "a"), Piece(("evidence", 2), "b")]),
        ("mcp__ledger__done", {}, []),
    ],
)
def test_each_tool_hands_over_the_text_it_would_write(tool, tool_input, expected):
    assert extract.pieces(tool, tool_input) == expected


@pytest.mark.parametrize(
    "tool_input, path",
    [
        ({"file_path": "/a.py", "path": "/b"}, "/a.py"),
        ({"notebook_path": "/n.ipynb"}, "/n.ipynb"),
        ({"path": "/c"}, "/c"),
        ({"file_path": 3}, ""),
        ({}, ""),
    ],
)
def test_the_target_path_comes_from_the_first_path_key(tool_input, path):
    assert extract.target_path(tool_input) == path


def test_a_rewrite_patches_only_the_changed_keys():
    assert extract.rewrite({"file_path": "a", "content": "old"}, {("content",): "new"}) == {"content": "new"}


def test_a_rewrite_keeps_the_other_edits_and_their_fields():
    tool_input = {"edits": [{"old_string": "a", "new_string": "b"}, {"old_string": "c", "new_string": "d"}]}
    patch = extract.rewrite(tool_input, {("edits", 1, "new_string"): "D"})
    assert patch == {"edits": [{"old_string": "a", "new_string": "b"}, {"old_string": "c", "new_string": "D"}]}
    assert tool_input["edits"][1]["new_string"] == "d"


def test_a_rewrite_replaces_evidence_items():
    tool_input = {"evidence": ["a", "b", "c"]}
    assert extract.rewrite(tool_input, {("evidence", 0): "A", ("evidence", 2): "C"}) == {"evidence": ["A", "b", "C"]}
    assert tool_input["evidence"] == ["a", "b", "c"]
