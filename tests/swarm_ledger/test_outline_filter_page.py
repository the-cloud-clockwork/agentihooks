import json
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.swarm_ledger.ledger_page import fulfill_events, is_events, ledger_state, loaded, serve_modules, shell_html

URL = "http://ledger.test/outline-filter"
PHASES = [{"id": "p1", "title": "Build it", "done": False}, {"id": "p2", "title": "Ship it", "done": False}]
HIDDEN_TASKS = [
    {"id": f"t{n}", "title": f"Old task {n}", "lane": "eng", "phase": "p1", "state": "done", "done": True}
    for n in range(12)
]
HIDDEN_TASKS[5]["title"] = "Rotate the quota router"
KEPT_TASKS = [
    {"id": f"t{n}", "title": f"Recent task {n}", "lane": "eng", "phase": "p2", "state": "done", "done": True}
    for n in range(12, 22)
]
OPEN_TASKS = [
    {"id": "t90", "title": "Write the outline filter", "lane": "eng", "phase": "p2", "state": "open"},
    {"id": "t91", "title": "Wire the router", "lane": "eng", "phase": "p1", "state": "claimed"},
]
QUESTIONS = [{"id": "q1", "text": "Which port?", "answers": [{"id": "a1", "by": "operator", "at": 1, "text": "9"}]}]
DOC = {
    "title": "Outline filter",
    "overview": "o",
    "phases": PHASES,
    "tasks": KEPT_TASKS + OPEN_TASKS,
    "questions": QUESTIONS,
    "followups": [{"id": "f1", "text": "Still wanted", "done": False}],
    "_meta": {
        "closed": {"tasks": {"hidden": 12, "kept": 10, "ids": [t["id"] for t in HIDDEN_TASKS], "plan_phases": []}}
    },
}


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


def done_rows(query):
    rows = list(reversed(HIDDEN_TASKS))
    text = query.get("q", "").casefold()
    return [row for row in rows if text in f"{row['id']} {row['title']}".casefold()]


@pytest.fixture
def ledger(browser):
    context = browser.new_context(viewport={"width": 1920, "height": 1080})
    html = shell_html()
    reads = []

    def answer(route):
        url = route.request.url
        if url == URL:
            return route.fulfill(body=html, content_type="text/html")
        if is_events(url):
            return fulfill_events(route, ledger=ledger_state(DOC))
        parts = urlsplit(url)
        if parts.path.startswith("/api/v1/ledgers/") and parts.path.endswith("/tasks"):
            query = {key: values[0] for key, values in parse_qs(parts.query).items()}
            reads.append(query)
            rows = done_rows(query)
            offset = int(query["offset"]) - (0 if "q" in query else 10)
            page = rows[offset : offset + int(query["limit"])]
            body = {"data": page, "revision": "0" * 64, "total": len(rows), "next_offset": None}
            return route.fulfill(body=json.dumps(body), content_type="application/json")
        return route.abort()

    context.route("**/*", answer)
    serve_modules(context)
    page = context.new_page()
    page.goto(URL)
    loaded(page)
    yield page, reads
    context.close()


def shown(page, section="tasks"):
    return page.locator(f"#ol-sec-{section} .ol-items a").evaluate_all(
        "links => links.filter(a => a.getClientRects().length).map(a => a.dataset.target.split('-').pop())"
    )


def test_typing_keeps_only_the_items_whose_title_or_id_matches(ledger):
    page, _ = ledger
    page.fill("#outline-filter", "OUTLINE filter")
    page.wait_for_function("() => document.querySelectorAll('#ol-sec-tasks .ol-items a').length === 1")
    assert shown(page) == ["t90"]
    assert page.locator("#ol-sec-questions").count() == 0
    page.fill("#outline-filter", "t91")
    page.wait_for_function(
        "() => document.querySelector('#ol-sec-tasks .ol-items a').dataset.target === 'item-tasks-t91'"
    )
    page.fill("#outline-filter", "")
    page.wait_for_function("() => document.querySelector('#ol-sec-questions')")


def test_the_state_filter_keeps_only_items_in_that_state(ledger):
    page, _ = ledger
    page.select_option("#outline-state", "claimed")
    page.wait_for_function("() => document.querySelectorAll('#ol-sec-tasks .ol-items a').length === 1")
    assert shown(page) == ["t91"]
    page.select_option("#outline-state", "")
    page.wait_for_function("() => document.querySelectorAll('#ol-sec-tasks .ol-items a').length === 12")


def test_type_ahead_finds_a_done_task_not_yet_loaded_and_reveals_it(ledger):
    page, reads = ledger
    page.fill("#outline-filter", "quota router")
    link = page.locator('#ol-sec-tasks a[data-target="item-tasks-t5"]')
    link.wait_for()
    assert {"state": "done", "q": "quota router", "offset": "0", "limit": "20"} in reads
    assert link.evaluate("a => a.classList.contains('st-done')")
    link.click()
    page.wait_for_function(
        """() => { const el = document.querySelector('#item-tasks-t5'); if (!el) return false;
        const r = el.getBoundingClientRect(); return r.top >= 0 && r.bottom <= innerHeight; }"""
    )


def test_tasks_group_under_their_phase_titles_done_tasks_included(ledger):
    page, _ = ledger
    groups = page.locator("#ol-sec-tasks .ol-group").all_text_contents()
    assert groups == ["Build it", "Ship it"]
    order = page.locator("#ol-sec-tasks .ol-items > li").evaluate_all(
        "rows => rows.map(r => r.classList.contains('ol-group') ? r.textContent : r.querySelector('a').dataset.target.split('-').pop())"
    )
    assert order[:3] == ["Build it", "t91", "Ship it"]
    assert "t21" in order[order.index("Ship it") :]


def test_a_fresh_viewer_sees_the_answered_questions_closed_and_a_saved_choice_wins(ledger):
    page, _ = ledger
    assert not page.locator("#ol-sec-questions").evaluate("box => box.open")
    assert not page.locator("#questions-box").evaluate("box => box.open")
    assert page.locator("#ol-sec-phases").evaluate("box => box.open")
    assert page.locator("#phases-box").evaluate("box => box.open")
    stored = page.evaluate(
        """async () => {
          const main = document.querySelector("script[type=module][src$='/js/main.js']").src;
          const {FOLD_KEY} = await import(new URL("config.js", main));
          return JSON.parse(localStorage.getItem(FOLD_KEY) || "{}");
        }"""
    )
    assert "ol-sec-questions" not in stored and "questions-box" not in stored
    page.locator("#ol-sec-questions").evaluate("box => { box.open = true; }")
    page.click("#questions-box > summary")
    page.wait_for_function(
        """async () => {
          const main = document.querySelector("script[type=module][src$='/js/main.js']").src;
          const {FOLD_KEY} = await import(new URL("config.js", main));
          const saved = JSON.parse(localStorage.getItem(FOLD_KEY) || "{}");
          return saved["ol-sec-questions"] === true && saved["questions-box"] === true;
        }"""
    )
    page.reload()
    loaded(page)
    assert page.locator("#ol-sec-questions").evaluate("box => box.open")
    assert page.locator("#questions-box").evaluate("box => box.open")


def test_arrows_and_j_k_walk_the_visible_outline_links_and_enter_reveals(ledger):
    page, _ = ledger
    links = page.locator("#outline a[data-target]").evaluate_all(
        "links => links.filter(a => a.getClientRects().length).map(a => a.dataset.target)"
    )
    focused = "() => document.activeElement.dataset.target"
    page.locator(f'#outline a[data-target="{links[0]}"]').focus()
    page.keyboard.press("ArrowDown")
    assert page.evaluate(focused) == links[1]
    page.keyboard.press("j")
    assert page.evaluate(focused) == links[2]
    page.keyboard.press("k")
    page.keyboard.press("ArrowUp")
    assert page.evaluate(focused) == links[0]
    target = next(link for link in links if link.startswith("item-tasks-"))
    page.locator(f'#outline a[data-target="{target}"]').focus()
    page.keyboard.press("Enter")
    page.wait_for_function(
        """id => { const el = document.getElementById(id); if (!el) return false;
        const r = el.getBoundingClientRect(); return r.top >= 0 && r.bottom <= innerHeight; }""",
        arg=target,
    )


def test_j_and_k_start_at_the_outline_and_walk_task_links_in_the_task_list(ledger):
    page, _ = ledger
    page.locator("body").press("j")
    assert page.evaluate("() => document.activeElement.matches('#outline a[data-target]')")
    page.click("#tasks-box > summary")
    first = page.locator("#tasks .item-title > a.task-id").first
    first.wait_for()
    ids = page.locator("#tasks .item-title > a.task-id").evaluate_all(
        "links => links.filter(a => a.getClientRects().length).map(a => a.textContent)"
    )
    first.focus()
    page.keyboard.press("ArrowDown")
    assert page.evaluate("() => document.activeElement.textContent") == ids[1]
    page.keyboard.press("k")
    assert page.evaluate("() => document.activeElement.textContent") == ids[0]


def test_typing_in_a_form_control_is_never_taken_for_navigation(ledger):
    page, _ = ledger
    page.focus("#outline-filter")
    page.keyboard.type("jk")
    assert page.input_value("#outline-filter") == "jk"
    assert page.evaluate("() => document.activeElement.id") == "outline-filter"
    page.focus("#jump-to")
    page.keyboard.press("j")
    page.keyboard.press("ArrowDown")
    assert page.evaluate("() => document.activeElement.id") == "jump-to"
    link = page.locator("#outline a[data-target]").first
    link.focus()
    page.keyboard.press("Control+j")
    assert page.evaluate("() => document.activeElement === document.querySelector('#outline a[data-target]')")


def test_the_filter_controls_carry_labels_and_a_visible_focus_ring(ledger):
    page, _ = ledger
    assert page.get_attribute("#outline-filter", "aria-label") == "Filter the outline by title or id"
    assert page.get_attribute("#outline-state", "aria-label") == "Filter the outline by state"
    page.focus("#outline-filter")
    page.keyboard.press("Tab")
    page.keyboard.press("Tab")
    ring = page.evaluate("() => getComputedStyle(document.activeElement).outlineStyle")
    assert ring != "none"
