import json
from pathlib import Path

import pytest

TEMPLATE = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger" / "template.html"
URL = "http://ledger.test/swarm-buildout"
COMMENT = {"id": "c1", "by": "operator", "at": 1, "text": "looks right"}
DOC = {
    "title": "Fold toggles",
    "overview": "o",
    "phases": [{"id": f"p{n}", "title": f"phase {n}", "done": False, "comments": [COMMENT]} for n in range(3)],
    "followups": [{"id": "f1", "text": "rotate the key", "done": False, "comments": [COMMENT]}],
    "notes": [
        {"id": "n1", "by": "operator", "at": 1, "text": "Keep replies here", "comments": [COMMENT]},
        {"id": "n2", "by": "operator", "at": 2, "text": "An older note"},
    ],
}
SECTIONS = ("sec-phases", "sec-tasks", "sec-questions", "sec-followups")
BLOCKED_STORAGE = (
    """Object.defineProperty(window, "localStorage", { get() { throw new Error("storage blocked"); } });"""
)


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


@pytest.fixture
def tab(browser):
    context = browser.new_context(viewport={"width": 1600, "height": 900})
    html = TEMPLATE.read_text(encoding="utf-8").replace("__LEDGER_DATA__", json.dumps(DOC))
    context.route(
        "**/*",
        lambda route: route.fulfill(body=html, content_type="text/html") if route.request.url == URL else route.abort(),
    )
    page = context.new_page()
    page.goto(URL)
    yield page
    context.close()


def comments(tab, section):
    return tab.evaluate(
        """(id) => {
          const sec = document.getElementById(id);
          return { labels: [...sec.querySelectorAll("button[data-comments]")].map((b) => b.textContent),
                   open: [...sec.querySelectorAll("details[data-key]")].map((d) => d.open) };
        }""",
        section,
    )


def outline(tab):
    return tab.evaluate(
        """() => ({ labels: [...document.querySelectorAll("#outline .ol-tools button")].map((b) => b.textContent),
                    open: [...document.querySelectorAll("#outline details.fold")].map((d) => d.open) })"""
    )


def settle(tab):
    tab.wait_for_timeout(50)


def test_every_comment_section_shows_one_control_and_the_outline_one(tab):
    for section in SECTIONS:
        assert len(comments(tab, section)["labels"]) == 1, section
    assert len(outline(tab)["labels"]) == 1


def test_notes_show_replies_under_the_note_and_remember_the_comments_toggle(tab):
    note = tab.locator("#item-notes-n1")
    assert note.locator(".entry-body").all_text_contents() == ["Keep replies here", COMMENT["text"]]
    assert tab.locator("#item-notes-n2 details[data-key]").count() == 1
    assert comments(tab, "sec-notes") == {"labels": ["Show all comments"], "open": [True, False]}
    tab.click("#sec-notes button[data-comments]")
    settle(tab)
    assert comments(tab, "sec-notes") == {"labels": ["Hide all comments"], "open": [True, True]}
    tab.click("#sec-notes button[data-comments]")
    settle(tab)
    assert comments(tab, "sec-notes") == {"labels": ["Show all comments"], "open": [False, False]}
    tab.reload()
    settle(tab)
    assert comments(tab, "sec-notes") == {"labels": ["Show all comments"], "open": [False, False]}
    tab.click("#sec-notes button[data-comments]")
    settle(tab)
    assert comments(tab, "sec-notes") == {"labels": ["Hide all comments"], "open": [True, True]}


def test_newly_added_note_immediately_has_a_comments_dropdown(tab):
    tab.locator("#notes > .thread > button.add").click()
    tab.locator("#notes textarea").fill("Another operator note")
    tab.locator("#notes textarea").press("Enter")
    assert tab.locator("#notes .entry-body", has_text="Another operator note").count() == 1
    assert tab.locator("#notes details[data-key]").count() == 3


def test_comment_control_label_flips_with_state_and_survives_a_reload(tab):
    assert comments(tab, "sec-phases") == {"labels": ["Hide all comments"], "open": [True, True, True]}
    tab.click("#sec-phases button[data-comments]")
    settle(tab)
    assert comments(tab, "sec-phases") == {"labels": ["Show all comments"], "open": [False, False, False]}
    assert comments(tab, "sec-followups") == {"labels": ["Hide all comments"], "open": [True]}
    tab.reload()
    settle(tab)
    assert comments(tab, "sec-phases") == {"labels": ["Show all comments"], "open": [False, False, False]}
    tab.click("#sec-phases button[data-comments]")
    settle(tab)
    tab.reload()
    settle(tab)
    assert comments(tab, "sec-phases") == {"labels": ["Hide all comments"], "open": [True, True, True]}


def test_one_dropdown_by_hand_is_remembered_and_flips_the_control_only_when_all_agree(tab):
    summaries = "#sec-phases details[data-key] > summary"
    tab.locator(summaries).nth(0).evaluate("(el) => el.click()")
    settle(tab)
    assert comments(tab, "sec-phases") == {"labels": ["Hide all comments"], "open": [False, True, True]}
    tab.reload()
    settle(tab)
    assert comments(tab, "sec-phases") == {"labels": ["Hide all comments"], "open": [False, True, True]}
    tab.locator(summaries).nth(1).evaluate("(el) => el.click()")
    tab.locator(summaries).nth(2).evaluate("(el) => el.click()")
    settle(tab)
    assert comments(tab, "sec-phases") == {"labels": ["Show all comments"], "open": [False, False, False]}
    tab.reload()
    settle(tab)
    assert comments(tab, "sec-phases")["labels"] == ["Show all comments"]


def test_outline_control_label_flips_with_state_and_survives_a_reload(tab):
    first = outline(tab)
    assert first["labels"] == ["Collapse all"]
    assert first["open"] and all(first["open"])
    tab.click("#outline .ol-tools button")
    settle(tab)
    folded = outline(tab)
    assert folded["labels"] == ["Expand all"]
    assert not any(folded["open"])
    tab.reload()
    settle(tab)
    assert outline(tab) == folded
    tab.click("#outline .ol-tools button")
    settle(tab)
    tab.reload()
    settle(tab)
    assert outline(tab) == first


def test_page_renders_both_controls_when_storage_is_unavailable(browser):
    context = browser.new_context()
    html = TEMPLATE.read_text(encoding="utf-8").replace("__LEDGER_DATA__", json.dumps(DOC))
    context.add_init_script(BLOCKED_STORAGE)
    context.route(
        "**/*",
        lambda route: route.fulfill(body=html, content_type="text/html") if route.request.url == URL else route.abort(),
    )
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    try:
        page.goto(URL)
        settle(page)
        assert comments(page, "sec-phases")["labels"] == ["Hide all comments"]
        page.click("#sec-phases button[data-comments]")
        settle(page)
        assert comments(page, "sec-phases") == {"labels": ["Show all comments"], "open": [False, False, False]}
        assert outline(page)["labels"] == ["Collapse all"]
        assert errors == []
    finally:
        context.close()
