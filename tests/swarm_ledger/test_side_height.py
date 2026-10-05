import json
import subprocess
import unittest
from pathlib import Path

import pytest

TEMPLATE = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger" / "template.html"
DOC = {
    "title": "Sidebar height",
    "overview": "o",
    "phases": [{"id": f"p{n}", "title": f"phase {n}", "description": "d " * 60, "done": False} for n in range(40)],
}


def page():
    return TEMPLATE.read_text(encoding="utf-8")


def side_max_height(top, viewport, sticky):
    source = "function sideMaxHeight(" + page().split("  function sideMaxHeight(", 1)[1].split("\n  }\n", 1)[0] + "\n}"
    script = source + f"\nprocess.stdout.write(JSON.stringify(sideMaxHeight({top}, {viewport}, {sticky})));"
    return json.loads(subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True).stdout)


class SideMaxHeight(unittest.TestCase):
    def test_under_the_header_it_spans_from_its_top_to_the_window_bottom_less_the_margin(self):
        self.assertEqual(side_max_height(180, 600, 24), 600 - 180 - 24)

    def test_stuck_it_spans_from_the_sticky_top_to_the_window_bottom_less_the_margin(self):
        self.assertEqual(side_max_height(24, 600, 24), 600 - 24 - 24)
        self.assertEqual(side_max_height(23.6, 600, 24), 600 - 24 - 24)

    def test_never_negative(self):
        self.assertEqual(side_max_height(900, 600, 24), 0)


@pytest.fixture(scope="module")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as pw:
        try:
            chromium = pw.chromium.launch()
        except Exception as exc:
            pytest.skip(f"no chromium: {exc}")
        yield chromium
        chromium.close()


def rendered(browser, side_px):
    tab = browser.new_page(viewport={"width": 1300, "height": 600})
    html = page().replace("__LEDGER_DATA__", json.dumps(DOC))
    tab.set_content(html)
    tab.evaluate(
        """(px) => {
          const extra = document.createElement("section");
          extra.style.height = px + "px";
          document.querySelector(".side").append(extra);
          dispatchEvent(new Event("resize"));
        }""",
        side_px,
    )
    return tab


def measure(tab, scroll_y):
    tab.evaluate("(y) => { scrollTo(0, y); dispatchEvent(new Event('scroll')); }", scroll_y)
    tab.wait_for_timeout(50)
    return tab.evaluate(
        """() => {
          const side = document.querySelector(".side");
          const box = side.getBoundingClientRect();
          return { top: box.top, bottom: box.bottom, scroll: side.scrollHeight, client: side.clientHeight };
        }"""
    )


def test_tall_sidebar_ends_at_the_window_bottom_less_the_margin_scrolled_and_unscrolled(browser):
    tab = rendered(browser, 3000)
    try:
        top = measure(tab, 0)
        assert top["top"] > 24
        assert abs(top["bottom"] - (600 - 24)) <= 1
        scrolled = measure(tab, 1200)
        assert abs(scrolled["top"] - 24) <= 1
        assert abs(scrolled["bottom"] - (600 - 24)) <= 1
        assert scrolled["scroll"] > scrolled["client"]
    finally:
        tab.close()


def test_sidebar_that_fits_has_no_inner_scroll(browser):
    tab = rendered(browser, 100)
    try:
        for y in (0, 1200):
            box = measure(tab, y)
            assert box["scroll"] <= box["client"]
    finally:
        tab.close()
