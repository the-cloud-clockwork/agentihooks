from pathlib import Path

import pytest

from tests.swarm_ledger.ledger_page import ledger_state, loaded, serve_modules, shell_html

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
    html = shell_html()
    context.route(
        "**/*",
        lambda route: route.fulfill(body=html, content_type="text/html") if route.request.url == URL else route.abort(),
    )
    serve_modules(context, ledger_state(DOC))
    page = context.new_page()
    page.goto(URL)
    loaded(page)
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
    loaded(tab)
    tab.wait_for_timeout(50)


def test_every_comment_section_shows_one_control_and_the_outline_one(tab):
    for section in SECTIONS:
        assert len(comments(tab, section)["labels"]) == 1, section
    assert len(outline(tab)["labels"]) == 1


def test_notes_show_replies_under_the_note_and_remember_the_comments_toggle(tab):
    note = tab.locator("#item-notes-n1")
    note.locator("details[data-key] > summary").click()
    note.locator(".entry-body").nth(1).wait_for()
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
    errors = []
    tab.on("pageerror", lambda error: errors.append(str(error)))
    tab.locator("#notes > .thread > button.add").click()
    tab.locator("#notes textarea").fill("Another operator note")
    tab.locator("#notes textarea").press("Enter")
    assert tab.locator("#notes .entry-body", has_text="Another operator note").count() == 1
    assert tab.locator("#notes details[data-key]").count() == 3
    note = tab.locator("#notes > .thread > .entry").last
    note.locator("details summary").click()
    note.locator("button.add").click()
    note.locator("textarea").fill("A reply under the new note")
    note.locator("textarea").press("Enter")
    assert note.locator(".entry-body").all_text_contents() == ["Another operator note", "A reply under the new note"]
    assert errors == []


def test_every_comments_dropdown_starts_collapsed_on_a_fresh_load_and_after_a_reload(tab):
    for section in (*SECTIONS, "sec-notes"):
        assert not any(comments(tab, section)["open"]), section
    tab.reload()
    settle(tab)
    for section in (*SECTIONS, "sec-notes"):
        assert not any(comments(tab, section)["open"]), section


def test_comments_stored_by_the_old_open_by_default_rule_start_collapsed(browser):
    context = browser.new_context()
    context.add_init_script(
        """if (!sessionStorage.getItem("seeded")) {
          sessionStorage.setItem("seeded", "1");
          localStorage.setItem("plan-ledger:swarm-buildout:comments", JSON.stringify({ open: ["phases/p0", "phases/p1", "phases/p2", "followups/f1", "notes/n1"], closed: [] }));
          localStorage.setItem("plan-ledger:swarm-buildout:toggles", JSON.stringify({ "sec-phases": true, "sec-followups": true, "all-comments": true }));
        }"""
    )
    html = shell_html()
    context.route(
        "**/*",
        lambda route: route.fulfill(body=html, content_type="text/html") if route.request.url == URL else route.abort(),
    )
    serve_modules(context, ledger_state(DOC))
    page = context.new_page()
    try:
        page.goto(URL)
        settle(page)
        for section in ("sec-phases", "sec-followups", "sec-notes"):
            assert comments(page, section) == {
                "labels": ["Show all comments"],
                "open": [False] * len(comments(page, section)["open"]),
            }
        page.click("#sec-phases button[data-comments]")
        settle(page)
        page.reload()
        settle(page)
        assert comments(page, "sec-phases") == {"labels": ["Hide all comments"], "open": [True, True, True]}
    finally:
        context.close()


def test_comment_control_label_flips_with_state_and_survives_a_reload(tab):
    assert comments(tab, "sec-phases") == {"labels": ["Show all comments"], "open": [False, False, False]}
    assert comments(tab, "sec-followups") == {"labels": ["Show all comments"], "open": [False]}
    tab.click("#sec-phases button[data-comments]")
    settle(tab)
    assert comments(tab, "sec-phases") == {"labels": ["Hide all comments"], "open": [True, True, True]}
    tab.reload()
    settle(tab)
    assert comments(tab, "sec-phases") == {"labels": ["Hide all comments"], "open": [True, True, True]}
    tab.click("#sec-phases button[data-comments]")
    settle(tab)
    tab.reload()
    settle(tab)
    assert comments(tab, "sec-phases") == {"labels": ["Show all comments"], "open": [False, False, False]}


def test_one_dropdown_by_hand_is_remembered_and_flips_the_control_only_when_all_agree(tab):
    summaries = "#sec-phases details[data-key] > summary"
    tab.locator(summaries).nth(0).evaluate("(el) => el.click()")
    settle(tab)
    assert comments(tab, "sec-phases") == {"labels": ["Show all comments"], "open": [True, False, False]}
    tab.reload()
    settle(tab)
    assert comments(tab, "sec-phases") == {"labels": ["Show all comments"], "open": [True, False, False]}
    tab.locator(summaries).nth(1).evaluate("(el) => el.click()")
    tab.locator(summaries).nth(2).evaluate("(el) => el.click()")
    settle(tab)
    assert comments(tab, "sec-phases") == {"labels": ["Hide all comments"], "open": [True, True, True]}
    tab.reload()
    settle(tab)
    assert comments(tab, "sec-phases")["labels"] == ["Hide all comments"]


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
    html = shell_html()
    context.add_init_script(BLOCKED_STORAGE)
    context.route(
        "**/*",
        lambda route: route.fulfill(body=html, content_type="text/html") if route.request.url == URL else route.abort(),
    )
    serve_modules(context, ledger_state(DOC))
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    try:
        page.goto(URL)
        settle(page)
        assert comments(page, "sec-phases")["labels"] == ["Show all comments"]
        page.click("#sec-phases button[data-comments]")
        settle(page)
        assert comments(page, "sec-phases") == {"labels": ["Hide all comments"], "open": [True, True, True]}
        assert outline(page)["labels"] == ["Collapse all"]
        assert errors == []
    finally:
        context.close()
