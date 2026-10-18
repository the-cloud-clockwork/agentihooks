import re
from pathlib import Path

import pytest

from scripts.swarm_ledger import ledger_layout as layout
from tests.swarm_ledger.ledger_page import page_source

ROOT = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"


@pytest.fixture(autouse=True)
def fresh():
    layout.path().unlink(missing_ok=True)
    yield
    layout.path().unlink(missing_ok=True)


def test_every_table_on_the_ledger_page_is_one_the_column_grips_reach():
    body = page_source().split("<body>", 1)[1]
    tables = re.findall(r"<table\b[^>]*>", body)
    assert tables
    for table in tables:
        assert re.search(r'class="[^"]*\bsw-table\b', table), table
        assert re.search(r'\bid="[a-z][a-z0-9-]*"', table), table


DOCUMENT_TABLES = {"markdown.js"}


def test_no_script_builds_a_table_the_column_grips_cannot_reach():
    for script in (*ROOT.joinpath("static", "js").glob("*.js"), ROOT / "static" / "home" / "home.js"):
        if script.name in DOCUMENT_TABLES:
            continue
        source = script.read_text()
        for found in re.findall(r'h\("table",\s*\{[^}]*\}', source):
            assert "sw-table" in found and "id:" in found, (script.name, found)
        assert 'createElement("table")' not in source, script.name


def test_the_ledger_page_and_home_wire_column_grips_and_save_their_widths():
    ledger = ROOT.joinpath("static", "js", "layout.js").read_text()
    assert 'document.querySelectorAll("table.sw-table[id]")' in ledger
    assert "columns: { ...(layout.columns || {})" in ledger
    home = ROOT.joinpath("static", "home", "home.js").read_text()
    assert 'document.querySelectorAll(".row.head > *")' in home
    assert 'fetch("/api/layout", { method: "PUT"' in home


def test_column_widths_round_trip_beside_panel_sizes_and_reset_with_them():
    saved = layout.write(
        {"health-box": {"height": 200}, "columns": {"agents-table-9": [250.4, 60, 0], "home": [280, 56]}}
    )
    assert saved["columns"] == {"agents-table-9": [250, 60, 0], "home": [280, 56]}
    assert layout.read() == saved
    assert layout.write({}) == {}
    assert layout.read() == {}


@pytest.mark.parametrize(
    "columns",
    [{"Agents": [100]}, {"agents-table-9": []}, {"agents-table-9": [5000]}, {"agents-table-9": [True]}, ["x"]],
)
def test_bad_column_widths_are_refused(columns):
    with pytest.raises(ValueError):
        layout.write({"columns": columns})


def test_the_column_limits_are_inclusive_and_each_refusal_names_its_cause():
    widest = [layout.COLUMN_WIDTH[1]] * layout.MAX_COLUMNS
    tables = {f"t{n}": [layout.COLUMN_WIDTH[0]] for n in range(layout.MAX_TABLES - 1)}
    saved = layout.write({"columns": {**tables, "wide": widest}})
    assert saved["columns"]["wide"] == widest and len(saved["columns"]) == layout.MAX_TABLES
    cases = [
        ({f"t{n}": [40] for n in range(layout.MAX_TABLES + 1)}, "at most"),
        ({"Bad": [40]}, "unknown column table Bad"),
        ({"t": [40] * (layout.MAX_COLUMNS + 1)}, "t needs 1 to"),
        ({"t": [layout.COLUMN_WIDTH[1] + 1]}, "t column widths must be numbers"),
    ]
    for columns, message in cases:
        with pytest.raises(ValueError, match=message):
            layout.write({"columns": columns})


def test_rows_after_the_columns_entry_are_still_saved():
    saved = layout.write({"columns": {"t": [40]}, "health-box": {"height": 200}})
    assert saved == {"columns": {"t": [40]}, "health-box": {"height": 200}}
