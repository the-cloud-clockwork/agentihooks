import json
from pathlib import Path

import pytest

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
        html = TEMPLATE.read_text(encoding="utf-8").replace("__LEDGER_DATA__", json.dumps(DOC))
        tab.set_content(html)
        return tab.evaluate(
            """() => {
              document.getElementById("swarm-box").hidden = false;
              for (const [id, v] of [["cap-eng", 3], ["cap-ci", 1], ["cap-codex", 30]]) document.getElementById(id).value = v;
              document.getElementById("codex-split").textContent = "1/3 spawns · 33% actual";
              const box = (sel) => [...document.querySelectorAll(sel)].map((el) => {
                const r = el.getBoundingClientRect();
                return { left: r.left, right: r.right, width: r.width };
              });
              return {
                down: box('.sw-caps [data-swarm$="_down"]'),
                num: box(".sw-caps .sw-cap"),
                up: box('.sw-caps [data-swarm$="_up"]'),
                caps: document.querySelector(".sw-caps").getBoundingClientRect().right,
              };
            }"""
        )
    finally:
        tab.close()


def same(values):
    return max(values) - min(values) <= 0.5


@pytest.mark.parametrize("width", [1300, 390])
def test_caps_rows_put_minus_number_and_plus_in_straight_columns(browser, width):
    boxes = caps_boxes(browser, width)
    assert len(boxes["down"]) == len(boxes["num"]) == len(boxes["up"]) == 4
    assert same([b["left"] for b in boxes["down"]]), boxes["down"]
    assert same([b["left"] for b in boxes["num"]]), boxes["num"]
    assert same([b["width"] for b in boxes["num"]]), boxes["num"]
    assert same([b["left"] for b in boxes["up"]]), boxes["up"]
    assert max(b["right"] for b in boxes["up"]) <= boxes["caps"]
