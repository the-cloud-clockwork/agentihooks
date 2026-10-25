from pathlib import Path

import pytest

from tests.swarm_ledger.ledger_page import ledger_state, loaded, serve_modules, shell_html
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
    html = shell_html()
    context.route(
        "**/*",
        lambda route: (
            route.fulfill(json={})
            if "/api/" in route.request.url
            else route.fulfill(body=html, content_type="text/html")
        ),
    )
    serve_modules(context, ledger_state(DOC))
    tab = context.new_page()
    tab.goto(URL)
    loaded(tab)
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
        "sec-notes",
        "sec-questions",
        "sec-phases",
        "sec-tasks",
        "sec-followups",
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
    ids = ["home", "art-fab", "bell", "alert-fab", "to-top", "sync"]
    assert page.locator("#icon-strip > *").evaluate_all("els => els.map(el => el.id)") == ids
    boxes = [page.locator(f"#{item}").bounding_box() for item in ids]
    assert all(box and box["x"] == 16 for box in boxes)
    assert all(a["y"] + a["height"] < b["y"] for a, b in zip(boxes, boxes[1:]))
    assert all(page.locator(f"#{item} svg").count() == 1 for item in ids)
    assert all(page.locator(f"#{item}-badge").count() == 1 for item in ["art", "bell", "alert", "sync"])


@pytest.mark.parametrize("width", [1440, 390])
def test_chat_bubble_floats_bottom_right_and_opens_chat_with_its_unread_count(browser, width):
    doc = {**DOC, "chat": [{"id": f"m{i}", "by": "boss", "at": 10 + i, "text": f"note {i}"} for i in range(3)]}
    context = browser.new_context(viewport={"width": width, "height": 844})
    context.add_init_script('localStorage.setItem("plan-ledger:__LEDGER_SLUG__:chat-seen", "10")')
    html = shell_html()
    context.route(
        "**/*",
        lambda route: (
            route.fulfill(json={})
            if "/api/" in route.request.url
            else route.fulfill(body=html, content_type="text/html")
        ),
    )
    serve_modules(context, ledger_state(doc))
    tab = context.new_page()
    tab.goto(URL)
    loaded(tab)
    tab.set_default_timeout(1500)
    bubble = tab.locator("#chat-fab")
    assert tab.locator("#icon-strip #chat-fab").count() == 0
    assert bubble.evaluate("el => getComputedStyle(el).position") == "fixed"
    box = bubble.bounding_box()
    assert box["x"] + box["width"] == width - 16
    assert box["y"] + box["height"] == 844 - 16
    assert tab.locator("#chat-badge").inner_text() == "2"
    bubble.click()
    assert tab.locator("#chat-panel").is_visible()
    assert bubble.get_attribute("aria-expanded") == "true"
    panel = tab.locator("#chat-panel").bounding_box()
    assert panel["x"] + panel["width"] == width - 16
    assert panel["y"] + panel["height"] <= box["y"]
    assert tab.locator("#chat-badge").is_hidden()
    context.close()
