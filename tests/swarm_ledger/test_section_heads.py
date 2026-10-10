from pathlib import Path

import pytest

from tests.swarm_ledger.ledger_page import ledger_state, serve_modules, shell_html

TEMPLATE = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger" / "template.html"
URL = "http://ledger.test/swarm-buildout"
ANSWER = {"id": "a1", "by": "operator", "at": 1, "text": "yes"}
DOC = {
    "title": "Section heads",
    "overview": "o",
    "sources": ["plan.md", "design.md"],
    "priorities": [{"id": "pr1", "item": "phases/p0", "text": "first"}],
    "phases": [
        {"id": "p0", "title": "phase 0", "done": False},
        {"id": "p1", "title": "phase 1", "done": False},
        {"id": "p2", "title": "phase 2", "done": True},
        {"id": "p3", "title": "phase 3", "done": False, "out_of_scope": True},
    ],
    "tasks": [{"id": "t1", "title": "t1", "state": "open"}, {"id": "t2", "title": "t2", "state": "done"}],
    "questions": [
        {"id": "q1", "text": "which?", "answers": []},
        {"id": "q2", "text": "when?", "answers": [ANSWER]},
        {"id": "q3", "text": "who?", "answers": [ANSWER]},
    ],
    "notes": [
        {"id": "n1", "by": "operator", "at": 1, "text": "one"},
        {"id": "n2", "by": "operator", "at": 2, "text": "two"},
        {"id": "n3", "by": "operator", "at": 3, "text": "gone", "deleted": True},
    ],
    "followups": [{"id": "f1", "text": "rotate the key", "done": True}],
}
EXPECTED = {
    "sources": "· 2",
    "prio": "· 1",
    "phases": "· 2 open · 1 done",
    "tasks": "· 1 open · 1 done",
    "questions": "· 1 open · 2 answered",
    "notes": "· 2",
    "followups": "· 1 done",
}
SECTIONS = ("sources-box", "prio-box", "phases-box", "tasks-box", "questions-box", "notes-box", "followups-box")
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


def open_page(browser, doc, init_script=None):
    context = browser.new_context(viewport={"width": 1600, "height": 900})
    if init_script:
        context.add_init_script(init_script)
    html = shell_html()
    context.route(
        "**/*",
        lambda route: route.fulfill(body=html, content_type="text/html") if route.request.url == URL else route.abort(),
    )
    serve_modules(context, ledger_state(doc))
    page = context.new_page()
    page.goto(URL)
    rendered(page)
    return context, page


def rendered(page):
    """The status line reads saved once the stream opens, so wait for the page's own first-state promise instead."""
    page.wait_for_function(
        """async () => {
          const main = document.querySelector("script[type=module]").src;
          await (await import(new URL("sync.js", main))).loaded;
          return true;
        }"""
    )


@pytest.fixture
def tab(browser):
    context, page = open_page(browser, DOC)
    yield page
    context.close()


def counts(tab):
    return tab.evaluate(
        """(keys) => Object.fromEntries(keys.map((k) => {
          const el = document.getElementById(`${k}-count`);
          return [k, el && el.getClientRects().length ? el.textContent.trim() : null];
        }))""",
        list(EXPECTED),
    )


def sections(tab):
    return tab.evaluate(
        """(ids) => ({ labels: [...document.querySelectorAll("#sections-all")].map((b) => b.textContent),
                       open: ids.map((id) => document.getElementById(id).open) })""",
        list(SECTIONS),
    )


def settle(tab):
    tab.wait_for_timeout(50)


def test_every_header_shows_its_counts_folded_and_open(tab):
    assert counts(tab) == EXPECTED
    for box in SECTIONS:
        tab.evaluate("(id) => { document.getElementById(id).open = !document.getElementById(id).open; }", box)
    settle(tab)
    assert counts(tab) == EXPECTED


def test_an_empty_ledger_shows_no_counts(browser):
    context, page = open_page(browser, {"title": "empty", "overview": "o"})
    try:
        assert set(counts(page).values()) <= {"", None}
    finally:
        context.close()


def test_counts_follow_an_item_change(tab):
    tab.locator("#item-phases-p0 input[type=checkbox]").evaluate("(el) => el.click()")
    settle(tab)
    assert counts(tab)["phases"] == "· 1 open · 2 done"
    tab.locator("#followups-done > summary").click()
    tab.locator("#item-followups-f1 input[type=checkbox]").evaluate("(el) => el.click()")
    settle(tab)
    assert counts(tab)["followups"] == "· 1 open"


def test_sections_toggle_sits_under_the_overview_and_flips_every_section(tab):
    assert tab.evaluate(
        "() => document.querySelector('#ledger .sec-tools').contains(document.getElementById('sections-all'))"
    )
    first = sections(tab)
    assert first["labels"] == ["Expand all"]
    tab.click("#sections-all")
    settle(tab)
    assert sections(tab) == {"labels": ["Collapse all"], "open": [True] * len(SECTIONS)}
    tab.click("#sections-all")
    settle(tab)
    assert sections(tab) == {"labels": ["Expand all"], "open": [False] * len(SECTIONS)}
    assert tab.evaluate("() => document.getElementById('overview-box').open")


def test_sections_toggle_leaves_comments_and_outline_alone(tab):
    probe = """() => Object.fromEntries([...document.querySelectorAll("details[data-key], #outline details.fold")]
             .map((d) => [d.dataset.key || d.id, d.open]))"""
    before = tab.evaluate(probe)
    tab.click("#sections-all")
    tab.click("#sections-all")
    settle(tab)
    after = tab.evaluate(probe)
    assert before.items() <= after.items()
    assert all(v is False for k, v in after.items() if k not in before)


def test_sections_state_survives_a_reload(tab):
    tab.click("#sections-all")
    settle(tab)
    tab.reload()
    rendered(tab)
    assert sections(tab) == {"labels": ["Collapse all"], "open": [True] * len(SECTIONS)}
    tab.evaluate("() => { document.getElementById('notes-box').open = false; }")
    settle(tab)
    assert sections(tab)["labels"] == ["Collapse all"]
    tab.reload()
    rendered(tab)
    folded = sections(tab)
    assert folded["open"][SECTIONS.index("notes-box")] is False
    assert folded["open"].count(True) == len(SECTIONS) - 1


def test_sections_toggle_works_when_storage_is_unavailable(browser):
    context, page = open_page(browser, DOC, BLOCKED_STORAGE)
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    try:
        page.reload()
        rendered(page)
        assert counts(page) == EXPECTED
        page.click("#sections-all")
        settle(page)
        assert sections(page) == {"labels": ["Collapse all"], "open": [True] * len(SECTIONS)}
        assert errors == []
    finally:
        context.close()
