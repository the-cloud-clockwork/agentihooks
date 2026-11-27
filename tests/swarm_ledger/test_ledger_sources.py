"""Behaviour of the ledger source_add op: check and apply in scripts.swarm_ledger.ledger_sources."""

import pytest

from scripts.swarm_ledger import ledger_core, ledger_sources

NOW = 1_700_000_000_000

CHECK_MESSAGE = r"^source_add takes id, by \(an author name\) and source$"
SOURCE_MESSAGE = r"^source must be text of at most 1000 characters$"


def ctx():
    return ledger_core.Context({"rev": 0, "stamps": {}}, NOW)


def op(**over: object) -> dict[str, object]:
    return {"op": "source_add", "id": "s1", "by": "swarm", "source": "https://example.test/doctor", **over}


@pytest.mark.feature("ledger-sources-op")
def test_check_accepts_a_well_formed_op() -> None:
    assert ledger_sources.check(op()) is None


@pytest.mark.parametrize(
    "missing", ["op", "id", "by", "source"], ids=["missing-op", "missing-id", "missing-by", "missing-source"]
)
def test_check_refuses_a_missing_key(missing: str) -> None:
    bad = op()
    del bad[missing]
    with pytest.raises(ValueError, match=CHECK_MESSAGE):
        ledger_sources.check(bad)


@pytest.mark.feature("ledger-sources-op")
def test_check_refuses_an_extra_key() -> None:
    with pytest.raises(ValueError, match=CHECK_MESSAGE):
        ledger_sources.check({**op(), "extra": 1})


@pytest.mark.parametrize(
    "by",
    [7, None, "1swarm", "", "swarm name", "-swarm"],
    ids=["not-a-string", "none", "leading-digit", "empty", "space", "leading-hyphen"],
)
def test_check_refuses_an_author_that_does_not_match_the_pattern(by: object) -> None:
    with pytest.raises(ValueError, match=CHECK_MESSAGE):
        ledger_sources.check(op(by=by))


@pytest.mark.parametrize(
    "by",
    ["a", "swarm", "Eng.1", "user@host", "a-b_c", "a" * 64],
    ids=["single-letter", "plain", "dot-and-digit", "at-sign", "hyphen-and-underscore", "at-the-length-limit"],
)
def test_check_accepts_every_well_formed_author(by: str) -> None:
    assert ledger_sources.check(op(by=by)) is None


@pytest.mark.feature("ledger-sources-op")
def test_check_refuses_an_author_one_character_over_the_length_limit() -> None:
    with pytest.raises(ValueError, match=CHECK_MESSAGE):
        ledger_sources.check(op(by="a" * 65))


@pytest.mark.parametrize("source", ["", "   "], ids=["empty", "blank"])
def test_check_refuses_a_blank_source(source: str) -> None:
    with pytest.raises(ValueError, match=SOURCE_MESSAGE):
        ledger_sources.check(op(source=source))


@pytest.mark.parametrize("source", [None, 7], ids=["none", "not-a-string"])
def test_check_refuses_a_source_that_is_not_text(source: object) -> None:
    with pytest.raises(ValueError, match=SOURCE_MESSAGE):
        ledger_sources.check(op(source=source))


def test_check_accepts_a_source_at_the_character_limit() -> None:
    assert ledger_sources.check(op(source="x" * 1000)) is None


@pytest.mark.feature("ledger-sources-op")
def test_check_refuses_a_source_one_character_over_the_limit() -> None:
    with pytest.raises(ValueError, match=SOURCE_MESSAGE):
        ledger_sources.check(op(source="x" * 1001))


@pytest.mark.feature("ledger-sources-op")
def test_apply_appends_a_new_source_with_its_stamp_and_its_record_event() -> None:
    doc: dict[str, object] = {}
    context = ctx()
    assert ledger_sources.apply(doc, op(), context) is True
    assert doc["sources"] == ["https://example.test/doctor"]
    assert context.stamps == {"sources": {"at": NOW, "rev": 1, "by": "swarm"}}
    assert context.events == [
        {
            "rev": 1,
            "at": NOW,
            "by": "swarm",
            "kind": "source added",
            "target": "sources",
            "id": "s1",
            "text": "https://example.test/doctor",
        }
    ]


@pytest.mark.feature("ledger-sources-op")
def test_apply_leaves_a_source_already_present_alone() -> None:
    doc: dict[str, object] = {"sources": ["https://example.test/doctor"]}
    context = ctx()
    assert ledger_sources.apply(doc, op(), context) is True
    assert doc["sources"] == ["https://example.test/doctor"]
    assert context.stamps == {}
    assert context.events == []
