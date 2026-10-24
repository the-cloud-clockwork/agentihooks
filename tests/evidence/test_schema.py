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
    with pytest.raises(schema.SchemaError, match="'excerpt' is a required property"):
        schema.load_evidence(write(tmp_path, "evidence.json", {"items": [item]}))


@pytest.mark.parametrize(
    ("kind", "doc"),
    [
        ("case", {"intent": "x"}),
        ("case", {"intent": "x", "points": []}),
        ("case", {"intent": "", "points": ["p"]}),
        ("case", {"intent": "x", "points": ["p"], "agent": "engineer"}),
        ("evidence", {"items": []}),
        ("evidence", {"items": [{**ITEM, "excerpt": ""}]}),
        ("evidence", {"items": [{**ITEM, "model": "opus"}]}),
        ("evidence", {"items": [ITEM, ITEM]}),
    ],
)
def test_validate_refuses_schema_errors(kind, doc):
    with pytest.raises(schema.SchemaError):
        schema.validate(kind, doc)


def test_validate_refuses_unknown_kind():
    with pytest.raises(ValueError, match="unknown schema verdict"):
        schema.validate("verdict", {})


def test_load_refuses_malformed_json(tmp_path):
    path = tmp_path / "case.json"
    path.write_text("{")
    with pytest.raises(schema.SchemaError, match="case.json is not JSON"):
        schema.load_case(path)


def test_shuffled_returns_new_list_with_same_items():
    items = [{"id": str(n)} for n in range(6)]
    result = schema.shuffled(items, 7)
    assert result is not items
    assert sorted(result, key=lambda item: item["id"]) == items
    assert schema.shuffled(items, 7) == result
    assert [item["id"] for item in items] == ["0", "1", "2", "3", "4", "5"]


def test_shuffled_never_keeps_input_order_across_fresh_seeds():
    items = [{"id": str(n)} for n in range(5)]
    same = sum(schema.shuffled(items) == items for _ in range(100))
    assert same <= 5


def test_fresh_seed_comes_from_secrets(monkeypatch):
    monkeypatch.setattr(schema.secrets, "randbits", lambda bits: bits * 1000)
    assert schema.fresh_seed() == 64000
