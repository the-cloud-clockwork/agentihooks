from pathlib import Path

import pytest

from tests.swarm_ledger.ledger_page import ledger_state, loaded, serve_modules, shell_html
from tests.swarm_ledger.test_caps_columns import browser as chromium_browser

browser = chromium_browser
ROOT = Path(__file__).resolve().parents[2]
URL = "http://127.0.0.1:8765/strip-panels"
PANELS = [("bell", "notif", "notifs"), ("art-fab", "art-panel", "art-list")]
DOC = {
    "title": "Strip panels",
    "overview": "Panels open beside their icon.",
    "phases": [
        {"id": f"p{i}", "title": f"Phase {i}", "description": "Recorded phase", "comments": []} for i in range(30)
    ],
    "notifications": [
        {
            "id": f"n{i}",
            "item": f"phases/p{i}",
            "label": "Comment",
            "by": "boss",
            "at": 1_000 + i,
            "text": f"Note on phase {i}",
        }
        for i in range(30)
    ],
    "artifacts": [
        {
            "id": f"a{i}",
            "title": f"Screenshot {i}",
            "by": "engineer@1",
            "task": "ip1",
            "at": 1_000 + i,
            "file": {"id": f"{i:064x}.md", "type": "text/markdown", "size": 10},
        }
        for i in range(30)
    ],
}


@pytest.fixture(params=[(1440, 900), (390, 844)], ids=["desktop", "phone"])
def tab(browser, request):
    width, height = request.param
    context = browser.new_context(viewport={"width": width, "height": height})
    html = shell_html()
    context.route(
        "**/*",
        lambda route: (
            route.fulfill(json={})
            if "/api/" in route.request.url
            else route.fulfill(body="# Shot", content_type="text/markdown")
            if "/artifacts/" in route.request.url
            else route.fulfill(body=html, content_type="text/html")
        ),
    )
    serve_modules(context, ledger_state(DOC))
    page = context.new_page()
    page.goto(URL)
    loaded(page)
    page.set_default_timeout(1500)
    yield page
    context.close()


@pytest.mark.parametrize("icon, panel, rows", PANELS)
def test_a_strip_panel_opens_beside_its_icon_and_scrolls_inside(tab, icon, panel, rows):
    tab.locator(f"#{icon}").click()
    assert tab.locator(f"#{panel}").is_visible()
    mark = tab.locator(f"#{icon}").bounding_box()
    strip = tab.locator("#icon-strip").bounding_box()
    box = tab.locator(f"#{panel}").bounding_box()
    view = tab.viewport_size
    assert box["y"] == pytest.approx(mark["y"], abs=0.5)
    assert box["x"] >= strip["x"] + strip["width"]
    assert box["x"] + box["width"] <= view["width"]
    assert box["y"] + box["height"] <= view["height"]
    listing = tab.locator(f"#{rows}")
    assert listing.evaluate("el => el.scrollHeight > el.clientHeight")
    listing.evaluate("el => { el.scrollTop = 120; }")
    assert listing.evaluate("el => el.scrollTop") == 120
    assert tab.evaluate("window.scrollY") == 0


def test_an_open_panel_follows_its_icon_when_the_page_resizes_or_the_strip_scrolls(tab):
    beside = """() => {
      const panel = document.getElementById("notif").getBoundingClientRect();
      const icon = document.getElementById("bell").getBoundingClientRect();
      return Math.abs(panel.top - icon.top) < 0.5 && panel.left >= document.getElementById("icon-strip").getBoundingClientRect().right;
    }"""
    bell, strip = tab.locator("#bell"), tab.locator("#icon-strip")
    bell.click()
    start = bell.bounding_box()["y"]
    tab.set_viewport_size({"width": {1440: 390, 390: 1440}[tab.viewport_size["width"]], "height": 320})
    moved = bell.bounding_box()["y"]
    assert moved == pytest.approx(start, abs=0.5)
    tab.wait_for_function(beside)
    assert strip.evaluate("el => el.scrollHeight > el.clientHeight")
    strip.evaluate("el => { el.scrollTop = 40; }")
    tab.wait_for_function(beside)
    assert bell.bounding_box()["y"] == pytest.approx(moved - 40, abs=0.5)


@pytest.mark.parametrize("icon, panel, rows", PANELS)
def test_a_strip_panel_closes_on_its_icon_escape_and_a_press_outside(tab, icon, panel, rows):
    button, box = tab.locator(f"#{icon}"), tab.locator(f"#{panel}")
    button.click()
    button.click()
    assert box.is_hidden()
    assert button.get_attribute("aria-expanded") == "false"
    button.click()
    tab.keyboard.press("Escape")
    assert box.is_hidden()
    button.click()
    box.locator(f"#{rows} .notif-meta").first.click(position={"x": 2, "y": 2})
    assert box.is_visible()
    tab.mouse.click(4, tab.viewport_size["height"] - 4)
    assert box.is_hidden()
    assert button.get_attribute("aria-expanded") == "false"
    other_icon, other_panel = [(i, p) for i, p, _ in PANELS if i != icon][0]
    button.click()
    tab.locator(f"#{other_icon}").click()
    assert box.is_hidden()
    assert tab.locator(f"#{other_panel}").is_visible()


def test_the_artifacts_panel_stays_open_while_its_viewer_is_used(tab):
    tab.locator("#art-fab").click()
    tab.locator("#art-list .art-title").first.click()
    viewer = tab.get_by_role("dialog", name="Artifact viewer")
    viewer.locator(".art-head strong").click()
    viewer.get_by_role("button", name="Close artifact").click()
    assert viewer.count() == 0
    assert tab.locator("#art-panel").is_visible()
