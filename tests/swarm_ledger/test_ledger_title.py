import pytest

from scripts.swarm_ledger import ledger_title


class Ctx:
    def __init__(self):
        self.stamps, self.records = [], []

    def stamp(self, field, by):
        self.stamps.append((field, by))

    def record(self, by, kind, target, **fields):
        self.records.append((by, kind, target, fields))


@pytest.mark.parametrize(
    "op",
    [
        {"op": "title_set", "id": "t", "text": "x" * ledger_title.MAX_TITLE},
        {"op": "overview_set", "id": "o", "text": ""},
        {"op": "overview_set", "id": "o", "text": "y" * ledger_title.MAX_OVERVIEW},
    ],
)
def test_check_takes_a_title_or_overview_within_its_limit(op):
    ledger_title.check(op)


@pytest.mark.parametrize(
    "op",
    [
        {"op": "title_set", "id": "t", "text": " "},
        {"op": "title_set", "id": "t", "text": "x" * (ledger_title.MAX_TITLE + 1)},
        {"op": "title_set", "id": "t", "text": 5},
        {"op": "overview_set", "id": "o", "text": "y" * (ledger_title.MAX_OVERVIEW + 1)},
        {"op": "overview_set", "id": "o", "text": None},
        {"op": "overview_set", "id": "o", "text": "z", "by": "eng-1"},
    ],
)
def test_check_refuses_an_empty_long_wrong_typed_or_agent_sent_edit(op):
    with pytest.raises(ValueError):
        ledger_title.check(op)


@pytest.mark.parametrize(("op", "field"), [("title_set", "title"), ("overview_set", "overview")])
def test_apply_stores_the_trimmed_text_once_and_records_it(op, field):
    doc, ctx = {"title": "Old", "overview": "old"}, Ctx()
    assert ledger_title.apply(doc, {"op": op, "id": "e1", "text": "  New text  "}, ctx) is True
    assert doc[field] == "New text"
    assert ctx.stamps == [(field, "operator")]
    assert ctx.records == [("operator", f"{field} changed", field, {"id": "e1", "text": "New text"})]
    ledger_title.apply(doc, {"op": op, "id": "e2", "text": "New text"}, ctx)
    assert len(ctx.records) == 1
