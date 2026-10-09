from pathlib import Path

import pytest

from tests.swarm_ledger.ledger_page import ledger_state, shell_html, show

TEMPLATE = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger" / "template.html"
DOC = {"title": "Caps columns", "overview": "o", "phases": []}


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


def caps_boxes(browser, width):
    tab = browser.new_page(viewport={"width": width, "height": 900})
    try:
        html = shell_html()
        show(tab, html, ledger=ledger_state(DOC))
        return tab.evaluate(
            """() => {
              document.getElementById("swarm").hidden = false;
              for (const [id, v] of [["cap-eng", "3"], ["cap-ci", "1"], ["cap-plan", "1"], ["cap-compact", "600"], ["cap-effort_min", "medium"], ["cap-effort_max", "high"]]) document.getElementById(id).value = v;
              const box = (el) => { const r = el.getBoundingClientRect(); return { top: r.top, bottom: r.bottom, left: r.left, right: r.right }; };
              return {
                caps: [...document.querySelectorAll("#capacity-box .sw-cap:not(.sw-affinity)")].map((cap) => [...cap.children].map(box)),
                strip: box(document.getElementById("capacity-box")),
              };
            }"""
        )
    finally:
        tab.close()


@pytest.mark.parametrize("width", [1300, 390])
def test_each_cap_keeps_its_name_minus_value_and_plus_on_one_line_inside_the_strip(browser, width):
    boxes = caps_boxes(browser, width)
    assert len(boxes["caps"]) == 6
    for name, minus, value, plus in boxes["caps"]:
        middle = (value["top"] + value["bottom"]) / 2
        for part in (name, minus, plus):
            assert part["top"] <= middle <= part["bottom"]
        assert name["right"] <= minus["left"] <= minus["right"] <= value["left"] <= value["right"] <= plus["left"]
        assert plus["right"] <= boxes["strip"]["right"]
