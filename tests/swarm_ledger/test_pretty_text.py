import dataclasses
import datetime
import json

import pytest

from scripts.swarm_ledger import ledger_core as core


@dataclasses.dataclass
class Row:
    n: int


def stdlib(value):
    return json.dumps(value, indent=2, ensure_ascii=False)


@pytest.mark.parametrize(
    "value",
    [
        {"text": "café ready", "quote": 'say "hi"', "lines": "a\nb\tc d\x1f", "slash": "a/b\\c"},
        {"empty": [], "nothing": {}, "nested": [{"a": [1, 2, {"b": None}]}], "flags": [True, False]},
        {"numbers": [0, -1, 1791478974235, 0.5, 1.25, -3.75]},
        {"big": 2**70, "keys": {1: "one"}, "text": "café ready"},
        {1: "un café"},
        [],
        "plain",
    ],
)
def test_pretty_text_matches_the_stdlib_indented_text(value):
    assert core.pretty(value) == stdlib(value)


def test_pretty_text_uses_orjson_for_the_values_it_accepts(monkeypatch):
    seen = []
    real = core.orjson.dumps

    def spy(value, **options):
        seen.append(value)
        return real(value, **options)

    monkeypatch.setattr(core.orjson, "dumps", spy)
    assert core.pretty({"text": "café"}) == stdlib({"text": "café"})
    assert seen == [{"text": "café"}]


@pytest.mark.parametrize("value", [{"at": datetime.datetime(2026, 1, 1)}, {"row": Row(1)}])
def test_pretty_text_refuses_what_the_stdlib_refuses(value):
    with pytest.raises(TypeError):
        core.pretty(value)


def test_the_stored_ledger_is_written_with_the_fast_encoder(monkeypatch, tmp_path):
    calls = []
    real = core.pretty

    def counted(value):
        calls.append(value)
        return real(value)

    monkeypatch.setattr(core, "pretty", counted)
    assert (
        core.seed_text({"title": "<b>"}, 3) == "\n" + stdlib({"_rev": 3, "title": "<b>"}).replace("<", "\\u003c") + "\n"
    )
    assert calls
