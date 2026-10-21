import json
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.swarm_ledger.ledger_page import fulfill_events, is_events, ledger_state, loaded, serve_modules, shell_html

URL = "http://ledger.test/closed-items"
HIDDEN_TASKS = [
    {"id": f"t{n}", "title": f"Old task {n}", "lane": "eng", "state": "done", "done": True} for n in range(15)
]
KEPT_TASKS = [
    {"id": f"t{n}", "title": f"Recent task {n}", "lane": "eng", "state": "done", "done": True} for n in range(15, 25)
]
OPEN_TASKS = [
    {"id": "t90", "title": "Open task", "lane": "eng", "state": "open", "done": False, "depends_on": ["t3"]},
    {"id": "t91", "title": "Other open task", "lane": "eng", "state": "claimed", "done": False},
]
HIDDEN_PHASES = [{"id": f"p{n}", "title": f"Old phase {n}", "done": True} for n in range(3)]
KEPT_PHASES = [{"id": f"p{n}", "title": f"Recent phase {n}", "done": True} for n in range(3, 13)]
OPEN_PHASES = [{"id": "p90", "title": "Open phase", "done": False, "depends_on": ["p1"]}]
DOC = {
    "title": "Closed items",
    "overview": "o",
    "phases": KEPT_PHASES + OPEN_PHASES,
    "tasks": KEPT_TASKS + OPEN_TASKS,
    "questions": [{"id": "q1", "text": "Still asked", "answers": []}],
    "followups": [{"id": "f1", "text": "Still wanted", "done": False}],
    "_meta": {
        "closed": {
            "tasks": {"hidden": 15, "kept": 10, "ids": [t["id"] for t in HIDDEN_TASKS], "plan_phases": []},
            "phases": {"hidden": 3, "kept": 10, "ids": [p["id"] for p in HIDDEN_PHASES]},
        }
    },
}
STORE = {"tasks": HIDDEN_TASKS, "phases": HIDDEN_PHASES}


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
def ledger(browser):
    context = browser.new_context(viewport={"width": 1920, "height": 1080})
    html = shell_html()
    reads = []
    served = {"doc": ledger_state(DOC), "store": STORE}

    def answer(route):
        url = route.request.url
        if url == URL:
            return route.fulfill(body=html, content_type="text/html")
        if is_events(url):
            return fulfill_events(route, ledger=served["doc"])
        parts = urlsplit(url)
        name = parts.path.rsplit("/", 1)[1]
        if parts.path.startswith("/api/v1/ledgers/") and name in served["store"]:
            query = {key: values[0] for key, values in parse_qs(parts.query).items()}
            reads.append((name, query))
            rows = list(reversed(served["store"][name]))
            offset, limit = int(query["offset"]) - 10, int(query["limit"])
            page = rows[offset : offset + limit]
            body = {"data": page, "revision": "0" * 64, "total": len(rows) + 10, "next_offset": None}
            return route.fulfill(body=json.dumps(body), content_type="application/json")
        return route.abort()

    context.route("**/*", answer)
    serve_modules(context)
    page = context.new_page()
    page.goto(URL)
    loaded(page)
    page.click("#tasks-box > summary")
    page.click("#tasks-done > summary")
    page.locator("#tasks-done ol > li[id^=item-tasks-]").first.wait_for()
    yield page, reads, served
    context.close()


def done_ids(page):
    return page.locator("#tasks-done ol > li[id^=item-tasks-]").evaluate_all("rows => rows.map(r => r.id.slice(11))")


def more(page, scope, name):
    return page.locator(f"{scope} button[data-closed={name}]")


def test_the_done_fold_shows_the_last_ten_newest_first_and_counts_the_hidden(ledger):
    page, reads, _ = ledger
    assert done_ids(page) == [f"t{n}" for n in range(24, 14, -1)]
    assert page.locator("#tasks-done > summary").text_content() == "25 done"
    assert "25 done" in page.locator("#tasks-count").text_content()
    assert more(page, "#tasks-done", "tasks").text_content() == "Show 10 more done tasks · 15 left"
    assert reads == []


def test_show_more_loads_the_next_ten_from_the_server_then_the_rest(ledger):
    page, reads, _ = ledger
    more(page, "#tasks-done", "tasks").click()
    page.wait_for_function("() => document.querySelectorAll('#tasks-done ol > li[id^=item-tasks-]').length === 20")
    assert done_ids(page) == [f"t{n}" for n in range(24, 4, -1)]
    assert reads == [("tasks", {"state": "done", "offset": "10", "limit": "10"})]
    assert more(page, "#tasks-done", "tasks").text_content() == "Show 5 more done tasks · 5 left"
    assert page.locator("#tasks-done > summary").text_content() == "25 done"
    more(page, "#tasks-done", "tasks").click()
    page.wait_for_function("() => document.querySelectorAll('#tasks-done ol > li[id^=item-tasks-]').length === 25")
    assert reads[1] == ("tasks", {"state": "done", "offset": "20", "limit": "10"})
    assert more(page, "#tasks-done", "tasks").count() == 0
    assert page.locator("#tasks-done > summary").text_content() == "25 done"


def test_the_phases_section_and_the_stats_count_hidden_phases_and_offer_show_more(ledger):
    page, reads, _ = ledger
    assert page.locator("#phases > li[id^=item-phases-]").count() == 11
    assert "13 done" in page.locator("#phases-count").text_content()
    assert page.locator('[data-stat="phases"] dd').text_content().startswith("13 / 14")
    assert page.locator('[data-stat="tasks"] dd').text_content() == "25 / 27"
    more(page, "#phases", "phases").click()
    page.wait_for_function("() => document.querySelectorAll('#phases > li[id^=item-phases-]').length === 14")
    assert reads == [("phases", {"state": "done", "offset": "10", "limit": "10"})]
    assert more(page, "#phases", "phases").count() == 0


def test_a_hidden_done_dependency_reads_as_done(ledger):
    page, _, _ = ledger
    assert page.locator("#item-phases-p90 .phase-state").text_content() == "Building"
    assert "Waiting until" not in page.locator("#item-tasks-t90").text_content()


def test_the_outline_lists_the_held_items_and_loads_more_done_items(ledger):
    page, reads, _ = ledger
    outline = "#ol-sec-tasks"
    links = page.locator(f"{outline} .ol-items a")
    assert links.count() == 12
    assert "25 done" in page.locator(f"{outline} > summary a").text_content()
    more(page, outline, "tasks").click()
    page.wait_for_function("() => document.querySelectorAll('#ol-sec-tasks .ol-items a').length === 22")
    assert reads == [("tasks", {"state": "done", "offset": "10", "limit": "10"})]
    assert more(page, outline, "tasks").text_content() == "Show 5 more done · 5 left"


def test_the_show_more_button_wears_the_page_more_link_style(ledger):
    page, _, _ = ledger
    button = more(page, "#tasks-done", "tasks")
    assert button.evaluate("b => b.classList.contains('link') && b.parentElement.classList.contains('page-more')")
    style = button.evaluate(
        "b => { const s = getComputedStyle(b); return [s.textTransform, s.boxShadow, s.textShadow]; }"
    )
    assert style == ["uppercase", "none", "none"]


def serve(served, rev, **changes):
    meta = {**DOC["_meta"], **changes.pop("_meta", {})}
    served["doc"] = ledger_state({**DOC, **changes, "_meta": meta}, rev=rev)


def test_a_row_leaving_the_server_window_after_a_load_stays_and_the_next_page_follows_it(ledger):
    page, reads, served = ledger
    more(page, "#tasks-done", "tasks").click()
    page.wait_for_function("() => document.querySelectorAll('#tasks-done ol > li[id^=item-tasks-]').length === 20")
    newest = {"id": "t25", "title": "Newest task", "lane": "eng", "state": "done", "done": True}
    hidden = HIDDEN_TASKS + KEPT_TASKS[:1]
    served["store"] = {**STORE, "tasks": hidden}
    closed = {"tasks": {"hidden": 16, "kept": 10, "ids": [t["id"] for t in hidden], "plan_phases": []}}
    serve(
        served, 2, tasks=KEPT_TASKS[1:] + [newest] + OPEN_TASKS, _meta={"closed": {**DOC["_meta"]["closed"], **closed}}
    )
    page.wait_for_function("() => document.querySelector('#tasks-done #item-tasks-t25')")
    assert done_ids(page) == ["t25", *[f"t{n}" for n in range(24, 4, -1)]]
    assert page.locator("#tasks-done > summary").text_content() == "26 done"
    assert more(page, "#tasks-done", "tasks").text_content() == "Show 5 more done tasks · 5 left"
    more(page, "#tasks-done", "tasks").click()
    page.wait_for_function("() => document.querySelectorAll('#tasks-done ol > li[id^=item-tasks-]').length === 26")
    assert reads[1] == ("tasks", {"state": "done", "offset": "21", "limit": "10"})
    assert done_ids(page)[-5:] == ["t4", "t3", "t2", "t1", "t0"]


def test_reopening_a_loaded_row_keeps_the_next_page_aligned(ledger):
    page, reads, served = ledger
    more(page, "#tasks-done", "tasks").click()
    page.wait_for_function("() => document.querySelectorAll('#tasks-done ol > li[id^=item-tasks-]').length === 20")
    reopened = {**HIDDEN_TASKS[10], "state": "open", "done": False}
    hidden = [t for t in HIDDEN_TASKS if t["id"] != "t10"]
    served["store"] = {**STORE, "tasks": hidden}
    closed = {"tasks": {"hidden": 14, "kept": 10, "ids": [t["id"] for t in hidden], "plan_phases": []}}
    serve(served, 2, tasks=KEPT_TASKS + [reopened] + OPEN_TASKS, _meta={"closed": {**DOC["_meta"]["closed"], **closed}})
    page.wait_for_function("() => document.querySelectorAll('#tasks-done ol > li[id^=item-tasks-]').length === 19")
    assert page.locator("#tasks-done > summary").text_content() == "24 done"
    assert more(page, "#tasks-done", "tasks").text_content() == "Show 5 more done tasks · 5 left"
    more(page, "#tasks-done", "tasks").click()
    page.wait_for_function("() => document.querySelectorAll('#tasks-done ol > li[id^=item-tasks-]').length === 24")
    assert reads[1] == ("tasks", {"state": "done", "offset": "19", "limit": "10"})
    assert done_ids(page)[-5:] == ["t4", "t3", "t2", "t1", "t0"]


def test_a_priority_on_a_hidden_done_task_reads_done_not_removed(ledger):
    page, _, served = ledger
    priorities = [
        {"id": "pr1", "item": "tasks/t3", "text": "check"},
        {"id": "pr2", "item": "tasks/t999", "text": "gone"},
    ]
    serve(served, 2, priorities=priorities)
    page.wait_for_function("() => document.querySelectorAll('#priorities .prio').length === 2")
    assert page.locator("#priorities .prio-title").all_text_contents() == ["This task is done", "This task was removed"]


def test_a_plan_counts_its_hidden_phases(ledger):
    page, _, served = ledger
    phases = [{**p, "plan": "plans/pl1"} for p in KEPT_PHASES] + OPEN_PHASES
    closed = {"phases": {**DOC["_meta"]["closed"]["phases"], "groups": {"plans/pl1": 2, "unplanned": 1}}}
    serve(
        served,
        2,
        plans=[{"id": "pl1", "title": "Plan one"}],
        phases=phases,
        _meta={"closed": {**DOC["_meta"]["closed"], **closed}},
    )
    page.wait_for_function("() => document.querySelector('#item-plans-pl1 .plan-count')")
    assert page.locator("#item-plans-pl1 .plan-count").text_content() == "12 phases"
    assert page.locator("#phases-unplanned .plan-count").text_content() == "2 phases"
