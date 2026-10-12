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
    message = (
        "is the operator's" if "by" in op else ("needs an overview" if op["op"] == "overview_set" else "needs a title")
    )
    with pytest.raises(ValueError, match=f"^{op['op']} {message}"):
        ledger_title.check(op)


def test_the_refusals_name_the_operation_and_its_limit():
    with pytest.raises(ValueError, match=r"^overview_set is the operator's and takes only id and text$"):
        ledger_title.check({"op": "overview_set", "id": "o", "text": "z", "by": "eng-1"})
    with pytest.raises(ValueError, match=rf"^title_set needs a title of 1 to {ledger_title.MAX_TITLE} characters$"):
        ledger_title.check({"op": "title_set", "id": "t", "text": ""})
    with pytest.raises(
        ValueError, match=rf"^overview_set needs an overview of at most {ledger_title.MAX_OVERVIEW} characters$"
    ):
        ledger_title.check({"op": "overview_set", "id": "o", "text": None})


@pytest.mark.parametrize(("op", "field"), [("title_set", "title"), ("overview_set", "overview")])
def test_apply_stores_the_trimmed_text_once_and_records_it(op, field):
    doc, ctx = {"title": "Old", "overview": "old"}, Ctx()
    assert ledger_title.apply(doc, {"op": op, "id": "e1", "text": "  New text  "}, ctx) is True
    assert doc[field] == "New text"
    assert ctx.stamps == [(field, "operator")]
    assert ctx.records == [("operator", f"{field} changed", field, {"id": "e1", "text": "New text"})]
    ledger_title.apply(doc, {"op": op, "id": "e2", "text": "New text"}, ctx)
    assert len(ctx.records) == 1
