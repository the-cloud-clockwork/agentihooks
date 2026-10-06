from collections import Counter
from pathlib import Path

import pytest

from scripts.swarm_ledger import artifact_sanity as sanity
from tests.swarm_ledger.test_artifacts import SVG
from tests.swarm_ledger.test_caps_columns import browser as chromium_browser

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "artifacts"
AUDIT = FIXTURES / "impeccable-audit-summary.md"
DELIVERY = FIXTURES / "realtime-delivery-findings.md"
NARROW = ".art-doc { max-width: 90ch; overflow-wrap: anywhere; }"
browser = chromium_browser


def good_markdown(**changes):
    measure = {
        "kind": "markdown",
        "page_overflow": False,
        "body_overflow": False,
        "close_visible": True,
        "json_folds": 0,
        "ch": 8.0,
        "width": 1440.0,
        "font_px": 13.0,
        "page_font_px": 13.0,
        "counts": {"h": 2, "li": 3, "table": 1, "pre": 1},
        "boxes_inside": True,
        "clipped": [],
        "broken_tokens": [],
        "tiny_cells": [],
    }
    return {**measure, **changes}


EXPECTED = Counter(h=2, li=3, table=1, pre=1)


class TestExpectedBlocks:
    def test_counts_the_real_audit_summary(self):
        assert sanity.expected_blocks(AUDIT.read_text()) == Counter(h=5, li=4, table=1, pre=0)

    def test_counts_the_real_delivery_report(self):
        assert sanity.expected_blocks(DELIVERY.read_text()) == Counter(h=14, li=25, table=4, pre=1)

    def test_fenced_lines_are_code_not_headings_or_lists(self):
        text = "# Title\n\n~~~\n# not a heading\n- not a list\n~~~\n\n1. one\n2) two\n+ three\n"
        assert sanity.expected_blocks(text) == Counter(h=1, li=3, table=0, pre=1)

    def test_an_unclosed_fence_runs_to_the_end(self):
        assert sanity.expected_blocks("```\n# a\n| x | y |\n|---|---|\n") == Counter(h=0, li=0, table=0, pre=1)

    def test_table_rows_end_at_the_first_line_without_a_pipe(self):
        text = "| a | b |\n| --- | --- |\n| 1 | 2 |\n- after\n\n| c |\n|:---|\n"
        assert sanity.expected_blocks(text) == Counter(h=0, li=1, table=2, pre=0)

    def test_a_pipe_line_without_a_rule_is_a_paragraph(self):
        assert sanity.expected_blocks("a | b\nc | d\n# h\n") == Counter(h=1, li=0, table=0, pre=0)

    def test_quotes_count_their_inner_blocks(self):
        text = "> ## Quoted\n> - one\n>- two\nplain\n"
        assert sanity.expected_blocks(text) == Counter(h=1, li=2, table=0, pre=0)

    def test_rules_blank_lines_and_paragraphs_add_nothing(self):
        text = "para one\ncontinues\n\n***\n___\n\r\nlast\r\n# end\r# again"
        assert sanity.expected_blocks(text) == Counter(h=2, li=0, table=0, pre=0)

    def test_a_list_after_a_paragraph_line_starts_a_list(self):
        assert sanity.expected_blocks("intro\n- a\n- b\n") == Counter(h=0, li=2, table=0, pre=0)

    def test_a_heading_needs_a_space_after_its_marks(self):
        assert sanity.expected_blocks("#tag\n####### seven\n###### six\n") == Counter(h=1, li=0, table=0, pre=0)

    def test_each_fence_counts_once_and_its_body_is_skipped_line_by_line(self):
        text = "```\none\n```\n# after\n~~~\n~~~\n# again"
        assert sanity.expected_blocks(text) == Counter(h=2, pre=2)

    def test_a_header_only_table_hands_the_next_line_back(self):
        assert sanity.expected_blocks("| a |\n|---|\n# h") == Counter(h=1, table=1)

    def test_a_table_and_a_pipe_line_may_end_the_text(self):
        assert sanity.expected_blocks("| a |\n|---|") == Counter(table=1)
        assert sanity.expected_blocks("# h\nx | y") == Counter(h=1)

    def test_table_body_rows_are_never_read_as_a_new_table(self):
        assert sanity.expected_blocks("| a |\n|---|\n| b |\n|---|\n") == Counter(table=1)

    def test_a_paragraph_swallows_pipe_lines_that_follow_it(self):
        assert sanity.expected_blocks("intro\n| a |\n|---|\n") == Counter()

    def test_carriage_returns_split_lines(self):
        assert sanity.expected_blocks("| a |\r\n|---|\r\n| 1 |\r# h") == Counter(h=1, table=1)


class TestCheck:
    def test_a_wide_well_formed_markdown_view_passes(self):
        assert sanity.check(good_markdown(), EXPECTED) == []

    def test_width_must_double_the_ninety_ch_baseline(self):
        assert sanity.check(good_markdown(width=1439.0), EXPECTED) == []
        assert sanity.check(good_markdown(width=1438.9), EXPECTED) == [
            "reading width 1439px is under 2 x 90ch (1440px)"
        ]

    def test_the_old_ninety_ch_column_fails(self):
        assert sanity.check(good_markdown(width=720.0), EXPECTED) == ["reading width 720px is under 2 x 90ch (1440px)"]

    def test_font_size_must_match_the_page(self):
        assert sanity.check(good_markdown(font_px=11.0), EXPECTED) == ["font size 11.0px differs from the page 13.0px"]

    def test_block_counts_must_match_the_source(self):
        failures = sanity.check(good_markdown(counts={"h": 1, "li": 3, "table": 0, "pre": 2}), EXPECTED)
        assert failures == [
            "h rendered 1, source has 2",
            "table rendered 0, source has 1",
            "pre rendered 2, source has 1",
        ]

    def test_markdown_without_expected_counts_compares_with_zero(self):
        assert sanity.check(good_markdown(counts={"h": 0, "li": 0, "table": 0, "pre": 0})) == []

    def test_overflow_and_clipping_fail(self):
        measure = good_markdown(
            page_overflow=True, body_overflow=True, close_visible=False, boxes_inside=False, clipped=["td", "pre", "td"]
        )
        assert sanity.check(measure, EXPECTED) == [
            "the page scrolls sideways",
            "the viewer body scrolls sideways",
            "the viewer close control is clipped",
            "a table or code block reaches past the viewer",
            "clipped text in pre, td",
        ]

    def test_broken_words_and_tiny_cells_fail(self):
        measure = good_markdown(broken_tokens=["`inbox_channel.py`", "`rewake.py`"], tiny_cells=["Async rewake hook"])
        assert sanity.check(measure, EXPECTED) == [
            "2 words broken mid word, first `inbox_channel.py`",
            "1 table cells under 8ch, first Async rewake hook",
        ]

    def test_json_needs_folds(self):
        base = {"page_overflow": False, "body_overflow": False, "close_visible": True}
        assert sanity.check({**base, "kind": "json", "json_folds": 3}) == []
        assert sanity.check({**base, "kind": "json", "json_folds": 0}) == ["the JSON viewer shows no folds"]

    def test_images_must_load_inside_the_viewer(self):
        base = {"page_overflow": False, "body_overflow": False, "close_visible": True, "kind": "image"}
        assert sanity.check({**base, "image": {"loaded": True, "inside": True}}) == []
        failed = ["the image did not load inside the viewer"]
        assert sanity.check({**base, "image": {"loaded": False, "inside": True}}) == failed
        assert sanity.check({**base, "image": {"loaded": True, "inside": False}}) == failed

    def test_an_empty_viewer_fails(self):
        measure = {"page_overflow": False, "body_overflow": False, "close_visible": True, "kind": "none"}
        assert sanity.check(measure) == ["the viewer rendered nothing"]


class TestPage:
    def test_each_file_is_listed_by_name_and_type_on_an_unreachable_port(self, tmp_path):
        svg = tmp_path / "flow.svg"
        svg.write_bytes(SVG)
        html = sanity.page_html([AUDIT, svg])
        rows = '[{"id": "art-0", "title": "impeccable-audit-summary.md", "file": {"id": "impeccable-audit-summary.md", '
        assert (
            rows
            + '"type": "text/markdown"}}, {"id": "art-1", "title": "flow.svg", "file": {"id": "flow.svg", "type": "image/svg+xml"}}]'
            in html
        )
        assert '{"title": "Artifact sanity", "artifacts": [' in html
        assert 'const PORT = "9";' in html
        for placeholder in ("DATA", "PALETTE", "PORT"):
            assert f"__LEDGER_{placeholder}__" not in html
        assert "--canvas" in html


@pytest.fixture
def fixture_files():
    return sorted(FIXTURES.iterdir())


@pytest.fixture
def narrow(tmp_path, monkeypatch):
    template = tmp_path / "template.html"
    wide = ".art-doc { max-width: 180ch; margin: 0 auto; line-height: 1.6; overflow-wrap: break-word; }"
    source = sanity.TEMPLATE.read_text()
    assert wide in source
    template.write_text(source.replace(wide, NARROW))
    (tmp_path / "palette.css").write_text((sanity.TEMPLATE.parent / "palette.css").read_text())
    monkeypatch.setattr(sanity, "TEMPLATE", template)


def test_real_artifacts_render_wide_and_readable_in_the_viewer(browser, fixture_files):
    report = sanity.run(browser, fixture_files)
    assert report == {path.name: [] for path in fixture_files}


def test_an_artifact_named_inside_another_name_opens_its_own_viewer(browser, tmp_path):
    files = [tmp_path / "notes.md", tmp_path / "old-notes.md"]
    files[0].write_text("# Notes\n")
    files[1].write_text("- old\n")
    assert sanity.run(browser, files) == {"notes.md": [], "old-notes.md": []}


def test_the_old_narrow_column_fails_width_and_compacts_the_delivery_table(browser, fixture_files, narrow):
    report = sanity.run(browser, fixture_files)
    for name in (AUDIT.name, DELIVERY.name):
        assert any(f.startswith("reading width") for f in report[name]), report[name]
    assert any("broken mid word" in f for f in report[DELIVERY.name]), report[DELIVERY.name]
    assert report["handoff-proposal.json"] == [] and report["handoff-flow.svg"] == []
