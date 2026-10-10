import pytest

from tests.swarm_ledger.ledger_page import ledger_state, shell_html, show
from tests.swarm_ledger.test_caps_columns import browser as chromium_browser

browser = chromium_browser


def phase(pid, plan, title, **extra):
    return {"id": pid, "title": title, "description": "", "done": False, "depends_on": [], "plan": plan, **extra}


def task(tid, phase_id, slice_id=""):
    return {"id": tid, "title": f"Task {tid}", "phase": phase_id, "lane": "eng", "state": "open", "slice": slice_id}


PLANS = [
    {"id": "hier", "title": "Plan hierarchy, freeze and the dispatcher", "artifact": "", "url": ""},
    {"id": "v2", "title": "Swarm v2", "artifact": "", "url": ""},
    {"id": "standalone", "title": "Standalone phases", "artifact": "", "url": ""},
]
PHASES = [
    phase("p3", "plans/v2", "Tier 1 foundations"),
    phase("p1", "plans/hier", "Plan hierarchy"),
    phase("p4", "plans/standalone", "Old work"),
    phase("p2", "plans/hier", "Freeze and focus", depends_on=["p1"]),
]
SLICES = [
    {"id": "hier.hy-resources", "phase": "phases/p1", "anchor": "hy-resources", "lines": "1-4"},
    {"id": "hier.hy-page", "phase": "phases/p1", "anchor": "hy-page", "lines": "5-8"},
    {"id": "hier.hy-empty", "phase": "phases/p1", "anchor": "hy-empty", "lines": ""},
]
TASKS = [
    task("t1", "p1", "slices/hier.hy-resources"),
    task("t2", "p1", "slices/hier.hy-page"),
    task("t3", "p1", "slices/hier.hy-page"),
    task("t4", "p3"),
]
DOC = {"title": "Plans", "overview": "", "plans": PLANS, "phases": PHASES, "slices": SLICES, "tasks": TASKS}
FLAT = {"title": "Flat", "overview": "", "phases": [phase("p1", "", "One"), phase("p2", "", "Two")]}


@pytest.fixture
def tab(browser):
    context = browser.new_context(viewport={"width": 1920, "height": 1080})
    page = context.new_page()
    page.set_default_timeout(3000)
    yield page
    context.close()


def fold_v2_and_reload(tab):
    tab.click("#item-plans-v2 > details.plan-fold > summary")
    tab.reload()
    tab.wait_for_function("() => document.getElementById('status').textContent !== 'loading'")
    assert tab.locator("#item-phases-p3").count() == 0


def plan_rows(tab):
    return tab.evaluate(
        """() => [...document.querySelectorAll("#phases > li.plan")].map((li) => ({
          id: li.id,
          title: li.querySelector(".plan-title").textContent,
          count: li.querySelector(".plan-count").textContent,
          phases: [...li.querySelectorAll("li[id^=item-phases-]")].map((p) => p.id),
        }))"""
    )


def test_two_plans_and_a_standalone_plan_render_three_plan_rows_in_plan_order(tab):
    show(tab, shell_html(), ledger=ledger_state(DOC))
    assert plan_rows(tab) == [
        {
            "id": "item-plans-hier",
            "title": "Plan hierarchy, freeze and the dispatcher",
            "count": "2 phases",
            "phases": ["item-phases-p1", "item-phases-p2"],
        },
        {"id": "item-plans-v2", "title": "Swarm v2", "count": "1 phase", "phases": ["item-phases-p3"]},
        {"id": "item-plans-standalone", "title": "Standalone phases", "count": "1 phase", "phases": ["item-phases-p4"]},
    ]


def test_each_plan_numbers_its_own_phases_from_one(tab):
    show(tab, shell_html(), ledger=ledger_state(DOC))
    numbers = tab.evaluate(
        """() => [...document.querySelectorAll("#phases > li.plan")].map((li) =>
          [...li.querySelectorAll("li[id^=item-phases-] > .row > .num")].map((n) => n.textContent))"""
    )
    assert numbers == [["1.", "2."], ["1."], ["1."]]


def test_a_phase_fold_lists_its_slices_with_task_counts(tab):
    show(tab, shell_html(), ledger=ledger_state(DOC))
    slices = tab.evaluate(
        """(id) => [...document.querySelectorAll(`#${id} .phase-slices .slice`)].map((s) => s.textContent)""",
        "item-phases-p1",
    )
    assert slices == ["hy-resources 1", "hy-page 2", "hy-empty 0"]
    assert tab.locator("#item-phases-p3 .phase-slices").count() == 0


def test_a_plan_row_folds_and_remembers_it_after_a_reload(tab):
    show(tab, shell_html(), ledger=ledger_state(DOC))
    tab.click("#item-plans-v2 > details.plan-fold > summary")
    assert tab.locator("#item-phases-p3").is_hidden()
    tab.reload()
    tab.wait_for_function("() => document.getElementById('status').textContent !== 'loading'")
    assert tab.evaluate("() => document.querySelector('#item-plans-v2 > details.plan-fold').open") is False
    assert tab.evaluate("() => document.querySelector('#item-plans-hier > details.plan-fold').open") is True


def test_a_phase_with_an_unknown_plan_lists_under_a_trailing_row(tab):
    doc = {**DOC, "phases": [*PHASES, phase("p5", "plans/gone", "Orphan"), phase("p6", "", "Loose")]}
    show(tab, shell_html(), ledger=ledger_state(doc))
    rows = plan_rows(tab)
    assert [row["id"] for row in rows] == [
        "item-plans-hier",
        "item-plans-v2",
        "item-plans-standalone",
        "phases-unplanned",
    ]
    assert rows[-1]["title"] == "Phases without a plan"
    assert rows[-1]["phases"] == ["item-phases-p5", "item-phases-p6"]


def test_a_plan_named_like_the_unplanned_row_keeps_its_own_row(tab):
    doc = {
        **DOC,
        "plans": [*PLANS, {"id": "unplanned", "title": "Real plan", "artifact": "", "url": ""}],
        "phases": [*PHASES, phase("p5", "plans/unplanned", "Planned"), phase("p6", "", "Loose")],
    }
    show(tab, shell_html(), ledger=ledger_state(doc))
    rows = {row["id"]: row["phases"] for row in plan_rows(tab)}
    assert rows["item-plans-unplanned"] == ["item-phases-p5"]
    assert rows["phases-unplanned"] == ["item-phases-p6"]


def test_a_plan_without_phases_shows_none(tab):
    doc = {**DOC, "plans": [*PLANS, {"id": "empty", "title": "Empty plan", "artifact": "", "url": ""}]}
    show(tab, shell_html(), ledger=ledger_state(doc))
    assert tab.locator("#item-plans-empty .plan-count").text_content() == "0 phases"
    assert tab.locator("#item-plans-empty li.empty").text_content() == "None."


def test_the_phases_section_folds_and_refills_its_plan_rows(tab):
    show(tab, shell_html(), ledger=ledger_state(DOC))
    tab.click("#phases-box > summary")
    tab.reload()
    tab.wait_for_function("() => document.getElementById('status').textContent !== 'loading'")
    assert tab.evaluate("() => document.getElementById('phases-box').open") is False
    assert tab.locator("#phases > li").count() == 0
    tab.click("#phases-box > summary")
    tab.wait_for_function("() => document.querySelectorAll('#phases > li.plan').length === 3")
    assert [row["id"] for row in plan_rows(tab)] == ["item-plans-hier", "item-plans-v2", "item-plans-standalone"]


def test_the_outline_opens_a_folded_plan_to_reach_its_phase(tab):
    show(tab, shell_html(), ledger=ledger_state(DOC))
    fold_v2_and_reload(tab)
    tab.click('#outline a[data-target="item-phases-p3"]')
    tab.wait_for_function("() => document.querySelector('#item-plans-v2 > details.plan-fold').open")
    assert tab.locator("#item-phases-p3").is_visible()


def test_show_all_comments_reaches_phases_inside_a_folded_plan(tab):
    show(tab, shell_html(), ledger=ledger_state(DOC))
    comments = "#item-phases-p3 details[data-key='phases/p3'] > summary"
    tab.click(comments)
    tab.click(comments)
    fold_v2_and_reload(tab)
    tab.click("#sec-phases button[data-comments]")
    tab.click("#item-plans-v2 > details.plan-fold > summary")
    tab.wait_for_function(
        "() => document.querySelector('#item-phases-p3 details[data-key=\"phases/p3\"]')?.open === true"
    )


def test_a_ledger_without_plans_keeps_the_flat_phase_list(tab):
    show(tab, shell_html(), ledger=ledger_state(FLAT))
    assert tab.locator("#phases > li.plan").count() == 0
    ids = tab.evaluate("() => [...document.querySelectorAll('#phases > li')].map((li) => li.id)")
    assert ids == ["item-phases-p1", "item-phases-p2"]
