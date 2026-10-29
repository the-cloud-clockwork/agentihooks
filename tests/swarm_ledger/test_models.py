from dataclasses import fields

import pytest
from jsonschema import Draft202012Validator

from scripts.swarm_ledger import ledger_core as core
from scripts.swarm_ledger import models
from scripts.swarm_ledger.api import resources, schemas
from scripts.swarm_ledger.repository import rows

WRONG = {str: 1, bool: "yes", list: "a"}


def test_models_keep_the_wire_shape():
    assert models.LISTS == {
        "tasks": (
            "title",
            "description",
            "phase",
            "lane",
            "state",
            "claimed_by",
            "issue_url",
            "pr_url",
            "done",
            "out_of_scope",
        ),
        "phases": ("title", "description", "done", "out_of_scope", "depends_on", "planning", "release"),
        "questions": ("text", "out_of_scope"),
        "followups": ("text", "done", "out_of_scope"),
        "plans": ("out_of_scope",),
    }
    assert list(models.THREADS.items()) == [
        ("tasks", ("comments",)),
        ("phases", ("comments",)),
        ("questions", ("comments", "answers")),
        ("followups", ("comments",)),
        ("notes", ("comments",)),
    ]
    assert models.COLLECTIONS == (
        "tasks",
        "phases",
        "questions",
        "followups",
        "notes",
        "chat",
        "priorities",
        "notifications",
        "artifacts",
        "artifact_trash",
        "sources",
        "alerts",
        "freezes",
        "plans",
        "slices",
    )
    assert models.RECORD_COLLECTIONS == frozenset(models.COLLECTIONS) - {"sources"}
    assert models.THREAD_NAMES == frozenset(("comments", "answers"))
    assert models.CHECKBOXES == ("done", "out_of_scope")
    assert models.TEXT_FIELDS == ("title", "overview", "orchestrator", "chat_instructions")
    assert models.field_type("phases", "depends_on") is list
    assert models.field_type("phases", "release") is bool
    assert models.field_type("tasks", "lane") is str


def test_model_helpers_read_the_dataclass_fields():
    assert models.item_type(list[models.Entry]) is models.Entry
    assert models.item_type(list[str]) is str
    assert models.item_type(str) is None
    assert models.threads(models.Question) == ("comments", "answers")
    assert models.threads(models.Plan) == ()
    assert models.shape(models.Question) == ("text", "out_of_scope")
    assert models.shape(models.Note) == ("by", "at", "text")
    assert models.checkboxes(models.Question) == ("out_of_scope",)


def test_core_validation_derives_its_fields_from_the_models():
    assert core.LISTS is models.LISTS
    assert core.THREADS is models.THREADS
    assert core.BOOL_FIELDS is models.CHECKBOXES
    for name, shape in models.LISTS.items():
        for field in shape:
            expected = models.field_type(name, field)
            with pytest.raises(ValueError, match=f"^{name}/a/{field} must be {expected.__name__}$"):
                core.validate({name: [{"id": "a", field: WRONG[expected]}]})
    for key in models.TEXT_FIELDS:
        with pytest.raises(ValueError, match=f"^{key} must be a string$"):
            core.validate({key: 1})


def test_api_schemas_derive_their_fields_from_the_models(monkeypatch):
    assert resources.COLLECTIONS is models.COLLECTIONS
    assert resources.THREADS is models.THREADS
    validator = Draft202012Validator(schemas.MUTATION)
    for name, model in models.MODELS.items():
        for segment in {field.name for field in fields(model)} | set(models.CHECKBOXES) | {""}:
            body = {
                "operation_id": "o",
                "ops": [],
                "guards": {},
                "id": "c",
                "changes": [{"path": f"{name}/a/{segment}", "value": True}],
            }
            assert validator.is_valid(body) is (segment in models.checkboxes(model)), (name, segment)
    assert not validator.is_valid({**body, "changes": [{"path": "sources/a/done", "value": True}]})
    monkeypatch.setattr(models, "MODELS", {"notes": models.Note, "plans": models.Plan})
    assert schemas.checkbox_path() == "^((plans)/[^/]+/(out_of_scope))$"


def test_storage_rows_derive_their_tables_from_the_models():
    assert rows.COLLECTIONS is models.RECORD_COLLECTIONS
    assert rows.THREAD_NAMES is models.THREAD_NAMES
    for name in models.COLLECTIONS:
        table = "resources" if name in models.RECORD_COLLECTIONS else "fields"
        assert rows.flatten({name: []})[rows.encode([name])][0] == table
    for thread in models.THREAD_NAMES:
        assert rows.flatten({"x": {thread: []}})[rows.encode(["x", thread])][0] == "threads"
    assert rows.flatten({"x": {"text": []}})[rows.encode(["x", "text"])][0] == "fields"
