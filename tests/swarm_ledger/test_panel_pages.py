import time

import pytest

from tests.swarm_ledger.ledger_page import PAGE_URL, chromium, ledger_state, loaded, serve_modules, shell_html, show

ROWS = 120
PAGE = 50


def panels_doc():
    now = int(time.time() * 1000)
    artifact = {"by": "engineer", "file": {"id": "f", "type": "text/markdown"}}
    return {
        "title": "Panels",
        "overview": "o",
        "tasks": [{"id": "t1", "title": "One", "lane": "eng", "state": "open"}],
        "notifications": [
            {"id": f"n{i}", "item": "tasks/t1", "at": i + 1, "label": "Comment", "by": "eng", "text": f"notice {i}"}
            for i in range(ROWS)
        ],
        "alerts": [
            {"id": f"a{i}", "text": f"alert {i}", "at": i + 1, "source": "size", "target": "ledger", "state": "open"}
            for i in range(ROWS)
        ],
        "artifacts": [{**artifact, "id": f"r{i}", "title": f"artifact {i}", "at": i + 1} for i in range(ROWS)],
        "artifact_trash": [
            {**artifact, "id": f"d{i}", "title": f"trashed {i}", "at": i + 1, "deleted_at": now - i}
            for i in range(ROWS)
        ],
    }


SWARM = {
    "config": {"state": "running", "max_eng": 3, "max_ci": 1},
    "tasks": {"claimed": 0},
    "agents": [{"name": "master", "lane": "master"}],
    "findings": [
        {"id": f"stale/eng-{i}", "kind": "stale", "subject": f"eng-{i}", "evidence": ["quiet"], "threshold": "30m"}
        for i in range(ROWS)
    ],
    "handoffs": [{"seat": f"eng-{i}@proof", "at": i + 1, "reason": "recycle"} for i in range(ROWS)],
}

NEWEST = range(ROWS - 1, -1, -1)
OLDEST = range(ROWS)
PANELS = [
    ("#bell", "#notifs", "more notifications", [f"notice-n{i}" for i in NEWEST]),
    ("#alert-fab", "#alerts", "more alerts", [f"alert-a{i}" for i in NEWEST]),
    ("#art-fab", "#art-list", "more artifacts", [f"artifact-r{i}" for i in NEWEST]),
    ("#art-fab", "#art-trash", "more trashed artifacts", [f"artifact-d{i}" for i in OLDEST]),
]
TABLES = [
    ("#health", "more findings", [f"finding-stale/eng-{i}" for i in OLDEST]),
    ("#swarm-handoffs", "more seats", [f"seat-eng-{i}@proof" for i in OLDEST]),
]
DEEP_LINKS = ["notice-n0", "alert-a0", "artifact-r0", "artifact-d119", "finding-stale/eng-119", "seat-eng-119@proof"]


@pytest.fixture(scope="module")
def browser():
    with chromium() as launched:
        yield launched


@pytest.fixture
def page(browser):
    tab = browser.new_page(viewport={"width": 1920, "height": 1080})
    yield tab
    tab.close()


def shown_ids(page, box):
    return page.locator(f"{box} > :not(.page-more)").evaluate_all("rows => rows.map((row) => row.id)")


def page_through(page, box, label, ids):
    more = page.locator(f"{box} > .page-more button")
    assert shown_ids(page, box) == ids[:PAGE]
    assert more.text_content() == f"Show {PAGE} {label} · {ROWS - PAGE} left"
    more.click()
    assert shown_ids(page, box) == ids[: 2 * PAGE]
    more.click()
    assert shown_ids(page, box) == ids
    assert more.count() == 0


@pytest.mark.parametrize(("opener", "box", "label", "ids"), PANELS)
def test_an_open_panel_with_more_rows_than_one_page_renders_one_page(page, opener, box, label, ids):
    show(page, shell_html(), ledger=ledger_state(panels_doc()), swarm=SWARM)
    page.click(opener)
    page_through(page, box, label, ids)


@pytest.mark.parametrize(("box", "label", "ids"), TABLES)
def test_a_swarm_table_with_more_rows_than_one_page_renders_one_page(page, box, label, ids):
    show(page, shell_html(), ledger=ledger_state(panels_doc()), swarm=SWARM)
    page.get_by_role("tab", name="Swarm", exact=False).click()
    page_through(page, box, label, ids)


@pytest.mark.parametrize("target", DEEP_LINKS)
def test_a_deep_link_past_the_first_page_opens_its_panel_and_reveals_the_row(page, target):
    page.route(PAGE_URL, lambda route: route.fulfill(body=shell_html(), content_type="text/html; charset=utf-8"))
    serve_modules(page, ledger_state(panels_doc()), SWARM)
    page.goto(f"{PAGE_URL}#{target}")
    loaded(page)
    row = page.locator(f"[id='{target}']")
    row.wait_for()
    assert row.is_visible()


def test_a_deep_link_reveals_its_finding_while_a_verdict_pick_has_focus(page):
    show(page, shell_html(), ledger=ledger_state(panels_doc()), swarm=SWARM)
    page.get_by_role("tab", name="Swarm", exact=False).click()
    page.locator("#health .hl-pick").first.focus()
    page.evaluate("() => { location.hash = 'finding-stale/eng-119'; }")
    row = page.locator("[id='finding-stale/eng-119']")
    row.wait_for(timeout=5000)
    assert row.is_visible()


def test_a_deep_link_reveals_its_alert_while_an_alert_outcome_has_focus(page):
    show(page, shell_html(), ledger=ledger_state(panels_doc()), swarm=SWARM)
    page.click("#alert-fab")
    page.locator("#alerts .alert-done").first.click()
    assert page.evaluate("() => document.activeElement.matches('#alerts input')")
    page.evaluate("() => { location.hash = 'alert-a0'; }")
    row = page.locator("[id='alert-a0']")
    row.wait_for(timeout=5000)
    assert row.is_visible()
