import json
from pathlib import Path

import pytest

from tests.swarm_ledger.test_caps_columns import browser as chromium_browser

browser = chromium_browser
ROOT = Path(__file__).resolve().parents[2]
URL = "http://ledger.test/tabs"
DOC = {
    "title": "Tab proof",
    "overview": "Read and decide, then steer the swarm.",
    "phases": [{"id": f"p{i}", "title": f"Phase {i}", "description": "Work"} for i in range(25)],
    "priorities": [{"id": "urgent", "item": "phases/p0", "text": "Choose the first phase"}],
    "tasks": [{"id": "one", "title": "Build the first feature", "state": "claimed"}],
}
SWARM = {
    "config": {"state": "running", "max_eng": 3, "max_ci": 1, "codex_share": 30},
    "tasks": {"claimed": 1},
    "agents": [{"name": "master", "lane": "master"}, {"name": "engineer", "lane": "eng", "task": "one"}],
    "findings": [],
    "spawns": {"codex": 6, "claude": 13},
}


@pytest.fixture
def tab(browser):
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    html = (ROOT / "scripts/swarm_ledger/template.html").read_text().replace("__LEDGER_DATA__", json.dumps(DOC))
    html = html.replace("__LEDGER_PALETTE__", (ROOT / "scripts/swarm_ledger/palette.css").read_text())

    def route(request):
        if "/api/swarm/" in request.request.url:
            request.fulfill(json=SWARM)
        elif request.request.url.startswith(URL):
            request.fulfill(body=html, content_type="text/html")
        else:
            request.abort()

    context.route("**/*", route)
    page = context.new_page()
    page.on("pageerror", lambda error: print(str(error)))
    page.goto(URL)
    yield page
    context.close()


def test_tabs_keep_the_shell_fixed_and_scroll_only_the_active_body(tab):
    assert tab.get_by_role("tab", name="Ledger").count() == 1
    assert tab.get_by_role("tab", name="Swarm").count() == 1
    tab.locator("#ledger").evaluate("el => el.scrollTop = 600")
    assert tab.locator("header").bounding_box()["y"] == 0
    assert tab.evaluate("window.scrollY") == 0
    assert tab.locator("#ledger").evaluate("el => el.scrollTop") == 600
    assert tab.evaluate("""() => [...document.querySelectorAll('body *')].filter(el => {
        const style = getComputedStyle(el);
        return el.getClientRects().length && ['auto', 'scroll'].includes(style.overflowY)
          && el.scrollHeight > el.clientHeight;
    }).map(el => el.id)""") == ["ledger"]


def test_tab_choice_hash_keyboard_and_scroll_positions_survive_switches(tab):
    tab.get_by_role("tab", name="Swarm").click()
    assert tab.locator("#swarm").is_visible()
    assert not tab.locator("#ledger").is_visible()
    assert tab.url.endswith("#swarm")
    tab.reload()
    assert tab.locator("#swarm").is_visible()
    tab.goto(URL)
    assert tab.locator("#swarm").is_visible()
    tab.goto(URL + "#ledger")
    assert tab.locator("#ledger").is_visible()
    tab.locator("#ledger").evaluate("el => el.scrollTop = 600")
    tab.get_by_role("tab", name="Ledger").focus()
    tab.keyboard.press("ArrowRight")
    assert tab.locator("#swarm").is_visible()
    tab.keyboard.press("Home")
    assert tab.locator("#ledger").is_visible()
    assert tab.locator("#ledger").evaluate("el => el.scrollTop") == 600
    tab.keyboard.press("End")
    assert tab.locator("#swarm").is_visible()
    tab.goto(URL + "#item-phases-p20")
    assert tab.locator("#ledger").is_visible()
    assert tab.locator("#item-phases-p20").is_visible()


@pytest.mark.parametrize("width", [1440, 390])
def test_swarm_contains_the_operational_panels_and_controls_do_not_cover_text(tab, width):
    tab.set_viewport_size({"width": width, "height": 844})
    tab.get_by_role("tab", name="Swarm").click()
    for name in ["Agents", "Needs you", "Capacity", "Swarm health", "Doctor", "Crew history"]:
        assert tab.locator("#swarm").get_by_text(name, exact=True).count() == 1
    assert tab.locator("#swarm #cap-eng").count() == 1
    assert tab.locator("#swarm #health").count() == 1
    assert tab.locator("#swarm #crew").count() == 1
    assert tab.locator("#swarm-restore-box").is_hidden()
    assert "Nothing waits on you." in tab.locator("#needs-you").inner_text()
    assert tab.locator("#swarm-agents-box").get_attribute("open") is not None
    assert tab.locator("#codex-split").inner_text() == (
        "Codex share: percent of new engineers started on Codex. So far 6 of 19 started on Codex (31%)."
    )
    assert tab.locator('[data-swarm="set"]').is_disabled()
    assert tab.locator("body").evaluate("el => el.scrollWidth") == width, tab.evaluate(
        "() => [...document.querySelectorAll('body *')].filter(el => el.getClientRects().length && el.getBoundingClientRect().right > innerWidth).map(el => [el.id, el.className, el.getBoundingClientRect().right])"
    )
    if width == 390:
        assert tab.locator("#chat-fab").evaluate("el => !!el.closest('header')")
        assert tab.locator("#bell").evaluate("el => !!el.closest('header')")


def test_plan_shape_shows_only_labelled_counts_for_open_dependencies(tab):
    sw = {
        **SWARM,
        "plan_shape": {
            "has_dependencies": True,
            "chain_length": 2,
            "parallel_width": 3,
            "summary": "Critical path should not appear",
            "warning": "Warnings should not appear as plan prose",
        },
    }
    tab.route("**/api/swarm/**", lambda route: route.fulfill(json=sw))
    tab.reload()
    tab.get_by_role("tab", name="Swarm").click()
    assert tab.locator("#swarm-plan-shape").inner_text() == "2\nChain\n3\nParallel"
    sw["plan_shape"]["has_dependencies"] = False
    tab.reload()
    tab.get_by_role("tab", name="Swarm").click()
    assert tab.locator("#swarm-plan-shape").is_hidden()


def test_agent_details_use_task_identity_and_the_published_progress_field(tab):
    doc = {
        **DOC,
        "tasks": [
            {"id": "one", "title": "Same title", "workspace_tail": {"latest_progress": "First task progress"}},
            {"id": "two", "title": "Same title", "workspace_tail": {"latest_progress": "Second task progress"}},
        ],
        "_meta": {"rev": 10},
    }
    sw = {**SWARM, "agents": [{"name": "engineer", "lane": "eng", "task": "two"}]}
    tab.route("**/api/**", lambda route: route.fulfill(json=sw if "/swarm/" in route.request.url else doc))
    tab.reload()
    tab.get_by_role("tab", name="Swarm").click()
    tab.locator("#agent-engineer > summary").click()
    assert "Second task progress" in tab.locator("#agent-engineer").inner_text()
    assert "First task progress" not in tab.locator("#agent-engineer").inner_text()


def test_global_comment_choice_survives_reload_and_the_next_click_collapses(tab):
    doc = {
        **DOC,
        "phases": [
            {"id": "p0", "title": "Review work", "comments": [{"id": "comment", "by": "operator", "text": "Read this"}]}
        ],
        "_meta": {"rev": 10},
    }
    tab.route("**/api/**", lambda route: route.fulfill(json=SWARM if "/swarm/" in route.request.url else doc))
    tab.reload()
    tab.locator("#comments-all").click()
    assert tab.locator("#comments-all").text_content() == "Hide all comments"
    tab.reload()
    assert tab.locator("#comments-all").text_content() == "Hide all comments"
    tab.locator("#comments-all").click()
    assert tab.locator("#comments-all").text_content() == "Show all comments"
    assert tab.locator("#sec-phases details[data-key]").get_attribute("open") is None
