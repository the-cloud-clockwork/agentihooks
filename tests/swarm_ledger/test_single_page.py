import json
from pathlib import Path

import pytest

from tests.swarm_ledger.test_caps_columns import browser

__all__ = ["browser"]
ROOT = Path(__file__).resolve().parents[2]
URL = "http://127.0.0.1:8765/layout-proof"
DOC = {
    "title": "Layout proof",
    "overview": "The ledger remains above all.",
    "phases": [
        {"id": f"p{i}", "title": f"Phase {i}", "description": "Recorded phase", "comments": []} for i in range(30)
    ],
    "questions": [],
    "notes": [],
    "followups": [],
    "tasks": [],
    "sources": [],
}


@pytest.fixture
def page(browser):
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    html = (ROOT / "scripts/swarm_ledger/template.html").read_text().replace("__LEDGER_DATA__", json.dumps(DOC))
    html = html.replace("__LEDGER_PALETTE__", (ROOT / "scripts/swarm_ledger/palette.css").read_text())
    context.route(
        "**/*",
        lambda route: (
            route.fulfill(json={})
            if "/api/" in route.request.url
            else route.fulfill(body=html, content_type="text/html")
        ),
    )
    tab = context.new_page()
    tab.goto(URL)
    tab.set_default_timeout(1500)
    yield tab
    context.close()


def test_approved_columns_overview_tabs_and_stats(page):
    assert page.locator(".workspace").evaluate("el => getComputedStyle(el).display") == "flex"
    assert page.locator(".workspace > *").evaluate_all("els => els.map(el => el.id)") == [
        "icon-strip",
        "outline",
        "main-content",
        "stats-column",
    ]
    assert page.locator("#main-content > *").evaluate_all("els => els.map(el => el.id)") == [
        "overview-box",
        "view-tabs",
        "ledger",
        "swarm",
    ]
    assert page.locator("#stats-column #stats-box").count() == 1
    assert page.locator("#stats-column #stats-sync").count() == 1
    assert page.locator("#ledger .col > section").evaluate_all("els => els.map(el => el.id)") == [
        "sec-priorities",
        "sec-questions",
        "sec-phases",
        "sec-tasks",
        "sec-followups",
        "sec-notes",
        "sec-sources",
    ]
    overview = page.locator("#overview-box").bounding_box()
    tabs = page.get_by_role("tablist").bounding_box()
    assert overview["y"] + overview["height"] <= tabs["y"]


def test_columns_scroll_independently(page):
    ids = ["icon-strip", "outline", "main-content", "stats-column"]
    for item in ids:
        page.locator(f"#{item}").evaluate("""el => {
            const filler = document.createElement('div');
            filler.style.height = '2000px';
            filler.style.flexShrink = '0';
            filler.textContent = 'Scroll witness';
            el.append(filler);
        }""")
    for item in ids:
        assert page.locator(f"#{item}").evaluate("el => getComputedStyle(el).overflowY") == "auto"
        page.locator(f"#{item}").evaluate("el => el.scrollTop = 150")
        assert page.locator(f"#{item}").evaluate("el => el.scrollTop") == 150
        assert page.evaluate("window.scrollY") == 0
        for other in ids:
            if other != item:
                assert page.locator(f"#{other}").evaluate("el => el.scrollTop") == 0
        page.locator(f"#{item}").evaluate("el => el.scrollTop = 0")


def test_icon_strip_keeps_home_first_and_all_counts(page):
    ids = ["home", "art-fab", "bell", "to-top", "sync", "chat-fab"]
    assert page.locator("#icon-strip > *").evaluate_all("els => els.map(el => el.id)") == ids
    boxes = [page.locator(f"#{item}").bounding_box() for item in ids]
    assert all(box and box["x"] == 16 for box in boxes)
    assert all(a["y"] + a["height"] < b["y"] for a, b in zip(boxes, boxes[1:]))
    assert all(page.locator(f"#{item} svg").count() == 1 for item in ids)
    assert all(page.locator(f"#{item}-badge").count() == 1 for item in ["art", "bell", "sync", "chat"])
