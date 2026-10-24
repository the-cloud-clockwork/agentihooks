import pytest

from tests.swarm_ledger.ledger_page import ledger_state, loaded, serve_modules, shell_html
from tests.swarm_ledger.test_caps_columns import browser as chromium_browser

browser = chromium_browser
URL = "http://127.0.0.1:8765/stats-drawer"
DOC = {
    "title": "Stats drawer",
    "overview": "Stats slide in from the right on narrow screens.",
    "phases": [
        {"id": f"p{i}", "title": f"Phase {i}", "description": "Recorded phase", "comments": []} for i in range(8)
    ],
}
SWIPE = """([x0, x1, dy]) => {
  const target = document.getElementById("main-content");
  const touch = (x, y) => new Touch({identifier: 1, target, clientX: x, clientY: y});
  target.dispatchEvent(new TouchEvent("touchstart", {bubbles: true, touches: [touch(x0, 400)], changedTouches: [touch(x0, 400)]}));
  target.dispatchEvent(new TouchEvent("touchend", {bubbles: true, touches: [], changedTouches: [touch(x1, 400 + dy)]}));
  return document.body.classList.contains("stats-open");
}"""


@pytest.fixture
def tab(browser):
    context = browser.new_context(viewport={"width": 390, "height": 844}, has_touch=True)
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
    page = context.new_page()
    page.goto(URL)
    loaded(page)
    page.set_default_timeout(1500)
    yield page
    context.close()


def is_open(tab):
    return tab.evaluate("document.body.classList.contains('stats-open')")


def test_the_arrow_opens_and_closes_the_stats_drawer(tab):
    toggle, panel = tab.locator("#stats-toggle"), tab.locator("#stats-column")
    assert toggle.is_visible()
    assert toggle.get_attribute("aria-expanded") == "false"
    assert toggle.get_attribute("aria-label") == "Show stats"
    assert panel.evaluate("el => getComputedStyle(el).visibility") == "hidden"
    toggle.click()
    assert is_open(tab)
    assert toggle.get_attribute("aria-expanded") == "true"
    assert toggle.get_attribute("aria-label") == "Hide stats"
    tab.wait_for_function(
        "() => document.getElementById('stats-column').getBoundingClientRect().right <= innerWidth + 0.5"
    )
    panel.click(position={"x": 40, "y": 40})
    assert is_open(tab)
    toggle.click()
    assert not is_open(tab)


def test_escape_and_a_tap_outside_close_the_drawer_without_reaching_the_page(tab):
    toggle = tab.locator("#stats-toggle")
    toggle.click()
    tab.keyboard.press("Escape")
    assert not is_open(tab)
    tab.keyboard.press("Escape")
    assert not is_open(tab)
    toggle.click()
    clicks = tab.evaluate("""() => {
      window.__pageClicks = 0;
      document.getElementById("main-content").addEventListener("click", () => { window.__pageClicks += 1; });
      return window.__pageClicks;
    }""")
    tab.mouse.click(20, 300)
    assert not is_open(tab)
    assert tab.evaluate("window.__pageClicks") == clicks
    tab.mouse.click(20, 300)
    assert tab.evaluate("window.__pageClicks") == clicks + 1


def test_a_left_swipe_from_the_right_half_opens_and_a_right_swipe_closes(tab):
    assert not tab.evaluate(SWIPE, [100, 10, 0])
    assert not tab.evaluate(SWIPE, [370, 340, 0])
    assert not tab.evaluate(SWIPE, [370, 200, 150])
    assert tab.evaluate(SWIPE, [370, 200, 0])
    assert tab.evaluate(SWIPE, [100, 20, 0])
    assert not tab.evaluate(SWIPE, [150, 320, 0])


def test_the_drawer_closes_and_its_arrow_hides_on_a_wide_screen(tab):
    tab.locator("#stats-toggle").click()
    tab.set_viewport_size({"width": 1440, "height": 900})
    tab.wait_for_function("() => !document.body.classList.contains('stats-open')")
    assert tab.locator("#stats-toggle").is_hidden()
    assert tab.locator("#stats-column").evaluate("el => getComputedStyle(el).position") != "fixed"
    assert not tab.evaluate(SWIPE, [1400, 1200, 0])
