import pytest

from scripts.evidence import schema

pytestmark = pytest.mark.unit

NOTE = {"point": "The judge returns a verdict", "missing": "no run shows a verdict"}


@pytest.mark.parametrize(
    "doc",
    [
        {"verdict": "pass", "notes": []},
        {"verdict": "enhance", "notes": [NOTE]},
        {"verdict": "retry", "notes": [NOTE, {**NOTE, "point": "The judge runs sealed"}]},
    ],
)
def test_valid_verdicts_load(doc):
    assert schema.validate("verdict", doc) == doc


def test_pass_with_notes_is_refused():
    with pytest.raises(schema.SchemaError) as refused:
        schema.validate("verdict", {"verdict": "pass", "notes": [NOTE]})
    assert str(refused.value) == f"verdict notes: {[NOTE]} is expected to be empty"


def test_unknown_verdict_is_refused():
    with pytest.raises(schema.SchemaError) as refused:
        schema.validate("verdict", {"verdict": "maybe", "notes": []})
    assert str(refused.value) == "verdict verdict: 'maybe' is not one of ['pass', 'retry', 'enhance']"


@pytest.mark.parametrize(
    ("doc", "message"),
    [
        ({"verdict": "retry"}, "verdict document: 'notes' is a required property"),
        (
            {"verdict": "retry", "notes": [], "score": 1},
            "verdict document: Additional properties are not allowed ('score' was unexpected)",
        ),
        ({"verdict": "retry", "notes": [{"point": "p"}]}, "verdict notes/0: 'missing' is a required property"),
        (
            {"verdict": "retry", "notes": [{"point": "", "missing": "m"}]},
            "verdict notes/0/point: '' should be non-empty",
        ),
        (
            {"verdict": "retry", "notes": [{**NOTE, "why": "x"}]},
            "verdict notes/0: Additional properties are not allowed ('why' was unexpected)",
        ),
    ],
)
def test_malformed_verdict_is_refused(doc, message):
    with pytest.raises(schema.SchemaError) as refused:
        schema.validate("verdict", doc)
    assert str(refused.value) == message
