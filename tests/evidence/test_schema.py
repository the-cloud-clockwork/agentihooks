import json

import pytest

from scripts.evidence import schema

pytestmark = pytest.mark.unit

CASE = {"intent": "Ship the judge", "points": ["The judge returns a verdict"]}
ITEM = {
    "id": "e1",
    "claim": "The judge returns a verdict",
    "source": "https://example.com/run/1",
    "excerpt": "verdict: pass",
    "retrieved_at": "2026-10-24T14:00:00Z",
}
SOURCE = r"^(https?://\S+|(?!.*(^|/)\.\.(/|$))[A-Za-z0-9_.][A-Za-z0-9_./-]*)$"


def write(tmp_path, name, doc):
    path = tmp_path / name
    path.write_text(json.dumps(doc))
    return path


def test_valid_case_and_evidence_set_load(tmp_path):
    assert schema.load_case(write(tmp_path, "case.json", CASE)) == CASE
    evidence = {"items": [ITEM, {**ITEM, "id": "e2", "source": "scripts/evidence/schema.py"}]}
    assert schema.load_evidence(write(tmp_path, "evidence.json", evidence)) == evidence


def test_missing_excerpt_is_refused(tmp_path):
    item = {key: value for key, value in ITEM.items() if key != "excerpt"}
    with pytest.raises(schema.SchemaError) as refused:
        schema.load_evidence(write(tmp_path, "evidence.json", {"items": [item]}))
    assert str(refused.value) == "evidence items/0: 'excerpt' is a required property"


def test_refusal_names_the_document_when_the_error_has_no_path():
    with pytest.raises(schema.SchemaError) as refused:
        schema.validate("case", {"intent": "x"})
    assert str(refused.value) == "case document: 'points' is a required property"


@pytest.mark.parametrize(
    ("kind", "doc", "message"),
    [
        ("case", {"intent": "x", "points": []}, "case points: [] should be non-empty"),
        ("case", {"intent": "", "points": ["p"]}, "case intent: '' should be non-empty"),
        (
            "case",
            {"intent": "x", "points": ["p"], "agent": "engineer"},
            "case document: Additional properties are not allowed ('agent' was unexpected)",
        ),
        ("evidence", {"items": []}, "evidence items: [] should be non-empty"),
        ("evidence", {"items": [{**ITEM, "excerpt": ""}]}, "evidence items/0/excerpt: '' should be non-empty"),
        (
            "evidence",
            {"items": [{**ITEM, "model": "opus"}]},
            "evidence items/0: Additional properties are not allowed ('model' was unexpected)",
        ),
        ("evidence", {"items": [ITEM, ITEM]}, f"evidence items: {[ITEM, ITEM]} has non-unique elements"),
        (
            "evidence",
            {"items": [ITEM, {**ITEM, "claim": "other"}, {**ITEM, "id": "e0"}, {**ITEM, "id": "e0", "claim": "x"}]},
            "evidence items: duplicate id e0, e1",
        ),
        (
            "evidence",
            {"items": [{**ITEM, "source": "see the run logs"}]},
            f"evidence items/0/source: 'see the run logs' does not match {SOURCE!r}",
        ),
        (
            "evidence",
            {"items": [{**ITEM, "source": "scripts/../../outside"}]},
            f"evidence items/0/source: 'scripts/../../outside' does not match {SOURCE!r}",
        ),
    ],
)
def test_validate_refuses_schema_errors(kind, doc, message):
    with pytest.raises(schema.SchemaError) as refused:
        schema.validate(kind, doc)
    assert str(refused.value) == message


def test_validate_refuses_unknown_kind():
    with pytest.raises(ValueError) as refused:
        schema.validate("ruling", {})
    assert str(refused.value) == "unknown schema ruling"


def test_load_refuses_malformed_json(tmp_path):
    path = tmp_path / "case.json"
    path.write_text("{")
    with pytest.raises(schema.SchemaError) as refused:
        schema.load_case(path)
    assert str(refused.value) == (
        "case.json is not JSON: Expecting property name enclosed in double quotes: line 1 column 2 (char 1)"
    )


def test_shuffled_returns_new_list_with_same_items():
    items = [{"id": str(n)} for n in range(6)]
    result = schema.shuffled(items, 7)
    assert result is not items
    assert sorted(result, key=lambda item: item["id"]) == items
    assert schema.shuffled(items, 7) == result
    assert [item["id"] for item in items] == ["0", "1", "2", "3", "4", "5"]


def test_shuffled_keeps_input_order_only_by_chance_across_fresh_seeds():
    items = [{"id": str(n)} for n in range(5)]
    same = sum(schema.shuffled(items) == items for _ in range(100))
    assert same <= 5


def test_fresh_seed_comes_from_secrets(monkeypatch):
    monkeypatch.setattr(schema.secrets, "randbits", lambda bits: bits * 1000)
    assert schema.fresh_seed() == 64000
