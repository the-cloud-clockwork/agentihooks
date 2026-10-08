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


def test_a_sync_writes_the_stored_document_through_the_fast_encoder(ledger_dir, monkeypatch):
    from scripts.swarm_ledger import new_ledger

    monkeypatch.setattr(core, "LEDGER_DIR", ledger_dir)
    slug = "pretty-2026-01-01"
    doc = new_ledger.build_doc(
        {"title": "Pretty", "overview": "o", "sources": [], "phases": [{"title": "one", "description": "d"}]}
    )
    html_path, json_path = core.paths(slug)
    html_path.write_text(new_ledger.render(doc, slug, 8765), encoding="utf-8")
    json_path.unlink(missing_ok=True)
    written = []
    real = core.pretty
    monkeypatch.setattr(core, "pretty", lambda value: written.append(value) or real(value))
    state, _ = core.sync(slug, ops=[{"op": "add", "thread": "chat", "id": "m1", "text": "café"}])
    assert any(value is not None and "_meta" in value for value in written if isinstance(value, dict))
    assert json_path.read_text(encoding="utf-8") == stdlib(state) + "\n"
