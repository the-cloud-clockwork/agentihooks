import pytest

from tests.swarm_ledger.ledger_page import ledger_state, loaded, reload_after_folds, serve_modules, shell_html

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
    loaded(page)


@pytest.fixture
def tab(browser, request):
    context, page = open_page(browser, DOC)
    if getattr(request, "param", 0):
        page.evaluate(
            """(delay) => {
              for (const el of document.querySelectorAll("[id$='-count']")) {
                const text = el.textContent;
                el.textContent = "";
                setTimeout(() => { el.textContent = text; }, delay);
              }
            }""",
            request.param,
        )
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


def slow_background_paint(tab):
    tab.evaluate(
        """() => {
          const post = scheduler.postTask.bind(scheduler);
          scheduler.postTask = (task, options) =>
            new Promise((done) => setTimeout(done, 100)).then(() => post(task, options));
        }"""
    )


@pytest.mark.parametrize("tab", [0, 250], indirect=True)
def test_every_header_shows_its_counts_folded_and_open(tab):
    tab.wait_for_function(
        "(keys) => keys.every(key => document.getElementById(key + '-count')?.textContent.trim())",
        arg=list(EXPECTED),
    )
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
    assert not tab.locator("#followups-box").evaluate("(box) => box.open")
    tab.click("#followups-box > summary")
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
    assert tab.evaluate("() => document.getElementById('overview-box').querySelector('summary') === null")


def test_sections_toggle_leaves_comments_and_outline_alone(tab):
    probe = """() => Object.fromEntries([...document.querySelectorAll("details[data-key], #outline details.fold")]
             .map((d) => [d.dataset.key || d.id, d.open]))"""
    before = tab.evaluate(probe)
    slow_background_paint(tab)
    tab.click("#sections-all")
    tab.wait_for_timeout(150)
    tab.click("#sections-all")
    tab.click("#sections-all")
    tab.wait_for_function(
        """(keys) => keys.every((k) => [...document.querySelectorAll("details[data-key], #outline details.fold")]
             .some((d) => (d.dataset.key || d.id) === k))""",
        arg=list(before),
    )
    after = tab.evaluate(probe)
    assert before.items() <= after.items()
    assert all(v is False for k, v in after.items() if k not in before)


def test_sections_state_survives_a_reload(tab):
    changed = tab.locator(".col > section[data-outline]:not(#sec-overview) > details.fold:not([open])").evaluate_all(
        "boxes => boxes.map(box => '#' + box.id).join(',')"
    )
    tab.click("#sections-all")
    settle(tab)
    reload_after_folds(tab, changed)
    assert sections(tab) == {"labels": ["Collapse all"], "open": [True] * len(SECTIONS)}
    tab.evaluate("() => { document.getElementById('notes-box').open = false; }")
    settle(tab)
    assert sections(tab)["labels"] == ["Collapse all"]
    reload_after_folds(tab, "#notes-box")
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
