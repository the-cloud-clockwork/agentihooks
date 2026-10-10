import pytest

from tests.swarm_ledger.ledger_page import ledger_state, loaded, serve_modules, shell_html
from tests.swarm_ledger.test_caps_columns import browser as chromium_browser

browser = chromium_browser
URL = "http://ledger.test/freeze-page"
AT = 1791600000000
SWARM = {"config": {"slug": "s", "state": "running", "max_eng": 1, "max_ci": 1}, "agents": [], "tasks": {}}


def phase(pid, plan, title):
    return {"id": pid, "title": title, "description": "", "done": False, "depends_on": [], "plan": plan}


def task(tid, phase_id, lane="eng", slice_id=""):
    return {"id": tid, "title": f"Task {tid}", "phase": phase_id, "lane": lane, "state": "open", "slice": slice_id}


def freeze(target, verb="freeze", by="operator"):
    return {"id": f"f-{target}-{verb}", "verb": verb, "target": target, "by": by, "at": AT, "reason": ""}


DOC = {
    "title": "Freeze",
    "overview": "",
    "plans": [{"id": "hier", "title": "Plan hierarchy"}, {"id": "v2", "title": "Swarm v2"}],
    "phases": [
        phase("p1", "plans/hier", "Plan hierarchy"),
        phase("p2", "plans/v2", "Tier 1"),
        phase("p3", "plans/v2", "Tier 2"),
        {**phase("p4", "plans/v2", "Old tier"), "out_of_scope": True},
    ],
    "slices": [{"id": "hier.hy-page", "phase": "phases/p1", "anchor": "hy-page", "lines": "1-4"}],
    "tasks": [
        task("t1", "p1", slice_id="slices/hier.hy-page"),
        task("t2", "p2"),
        task("t3", "p1", lane="ci"),
        {**task("t5", "p3"), "merged_into": "t1"},
    ],
}


@pytest.fixture
def page(browser, request):
    freezes = getattr(request, "param", [])
    context = browser.new_context(viewport={"width": 1920, "height": 1080})
    html = shell_html()
    sent = {"ops": [], "changes": []}

    def answer(route):
        if route.request.url == URL:
            return route.fulfill(body=html, content_type="text/html")
        if route.request.method == "PUT":
            body = route.request.post_data_json
            sent["ops"].extend(body.get("ops", []))
            sent["changes"].extend(body.get("changes", []))
        return route.abort()

    context.route("**/*", answer)
    serve_modules(context, ledger_state({**DOC, "freezes": freezes}), SWARM)
    tab = context.new_page()
    tab.set_default_timeout(3000)
    tab.on("dialog", lambda dialog: dialog.accept())
    tab.goto(URL)
    loaded(tab)
    tab.click("#tasks-box > summary")
    tab.locator("#item-tasks-t1").wait_for()
    yield tab, sent
    context.close()


def sent_ops(tab, sent, kind):
    for _ in range(40):
        if any(op["op"] == kind for op in sent["ops"]):
            break
        tab.wait_for_timeout(50)
    return [{k: v for k, v in op.items() if k != "id"} for op in sent["ops"] if op["op"] == kind]


def row_state(tab, selector):
    return tab.evaluate(
        """(sel) => {
          const li = document.querySelector(sel);
          const off = (q) => [...li.querySelectorAll(`:scope > .row ${q}`)].map((el) => el.disabled);
          return {
            frozen: li.classList.contains("frozen"),
            held: li.classList.contains("held"),
            flake: !!li.querySelector(":scope > .snowflake"),
            checkbox: off("input[type=checkbox]"),
            rank: off(".rank-pick"),
            verdicts: off(".approve, .deny"),
            scope: off(".scope"),
          };
        }""",
        selector,
    )


def freeze_text(tab, selector):
    return tab.locator(f"{selector} .freeze").first.text_content()


def test_every_plan_phase_slice_and_task_row_carries_a_freeze_button(page):
    tab, _ = page
    for selector in ("#item-plans-hier", "#item-phases-p1", "#item-slices-hier\\.hy-page", "#item-tasks-t1"):
        assert freeze_text(tab, selector) == "freeze"
    assert tab.locator("#item-plans-hier > details.plan-fold > summary .freeze").count() == 1
    assert tab.locator("#item-plans-hier > details.plan-fold > summary .scope").count() == 1


def test_freezing_a_plan_from_its_row_sends_the_freeze_and_disables_its_rows(page):
    tab, sent = page
    tab.click("#item-plans-v2 > details.plan-fold > summary .freeze")
    assert sent_ops(tab, sent, "freeze_set") == [{"op": "freeze_set", "verb": "freeze", "target": "plans/v2"}]
    tab.wait_for_function("() => document.querySelector('#item-phases-p2').classList.contains('frozen')")
    assert freeze_text(tab, "#item-plans-v2") == "unfreeze"
    assert row_state(tab, "#item-tasks-t2")["rank"] == [True]


@pytest.mark.parametrize("page", [[freeze("plans/v2")]], indirect=True)
def test_a_frozen_plan_dims_and_disables_everything_under_it_while_comments_stay_open(page):
    tab, _ = page
    assert tab.evaluate("() => document.querySelector('#item-plans-v2').classList.contains('frozen')") is True
    assert tab.locator("#item-plans-v2 > .snowflake").count() == 1
    for selector in ("#item-phases-p2", "#item-phases-p3"):
        state = row_state(tab, selector)
        assert (state["frozen"], state["flake"], state["checkbox"], state["verdicts"], state["scope"]) == (
            True,
            True,
            [True],
            [True, True],
            [True],
        )
        tab.click(f"{selector} > details[data-key] > summary")
        tab.click(f"{selector} > details[data-key] button.add")
        assert tab.locator(f"{selector} > details[data-key] textarea").first.is_enabled()
    task_state = row_state(tab, "#item-tasks-t2")
    assert (task_state["frozen"], task_state["flake"], task_state["rank"], task_state["verdicts"]) == (
        True,
        True,
        [True],
        [True, True],
    )
    assert row_state(tab, "#item-phases-p1")["frozen"] is False
    assert row_state(tab, "#item-tasks-t1")["rank"] == [False]
    assert freeze_text(tab, "#item-phases-p2") == "freeze"


@pytest.mark.parametrize("page", [[freeze("plans/v2")]], indirect=True)
def test_unfreeze_sends_the_clear_and_restores_the_rows(page):
    tab, sent = page
    tab.click("#item-plans-v2 > details.plan-fold > summary .freeze")
    assert sent_ops(tab, sent, "freeze_clear") == [{"op": "freeze_clear", "target": "plans/v2"}]
    tab.wait_for_function("() => !document.querySelector('#item-phases-p2').classList.contains('frozen')")
    assert row_state(tab, "#item-tasks-t2")["rank"] == [False]
    assert row_state(tab, "#item-phases-p2")["checkbox"] == [False]
    assert freeze_text(tab, "#item-plans-v2") == "freeze"


@pytest.mark.parametrize("page", [[freeze("phases/p1"), freeze("tasks/t2")]], indirect=True)
def test_unfreezing_a_parent_clears_the_freezes_under_it_on_the_page(page):
    tab, sent = page
    tab.click("#item-plans-hier > details.plan-fold > summary .freeze")
    tab.click("#item-plans-hier > details.plan-fold > summary .freeze")
    assert sent_ops(tab, sent, "freeze_clear") == [{"op": "freeze_clear", "target": "plans/hier"}]
    tab.wait_for_function("() => !document.querySelector('#item-phases-p1').classList.contains('frozen')")
    assert row_state(tab, "#item-tasks-t2")["frozen"] is True


@pytest.mark.parametrize("page", [[freeze("plans/hier", verb="focus")]], indirect=True)
def test_a_focus_puts_the_snowflake_on_items_outside_it_only(page):
    tab, _ = page
    outside = row_state(tab, "#item-phases-p2")
    assert (outside["held"], outside["flake"], outside["frozen"], outside["checkbox"]) == (True, True, False, [False])
    assert row_state(tab, "#item-tasks-t2")["held"] is True
    assert tab.locator("#item-plans-v2 > .snowflake").count() == 1
    for selector in ("#item-phases-p1", "#item-tasks-t1", "#item-tasks-t3"):
        state = row_state(tab, selector)
        assert (state["held"], state["flake"]) == (False, False)
    assert tab.locator("#item-plans-hier > .snowflake").count() == 0


@pytest.mark.parametrize("page", [[freeze("lane:ci")]], indirect=True)
def test_a_lane_freeze_holds_the_tasks_in_that_lane(page):
    tab, _ = page
    assert row_state(tab, "#item-tasks-t3")["frozen"] is True
    assert row_state(tab, "#item-tasks-t1")["frozen"] is False


@pytest.mark.parametrize("page", [[freeze("slices/hier.hy-page")]], indirect=True)
def test_a_frozen_slice_holds_its_tasks_and_reads_unfreeze(page):
    tab, _ = page
    assert freeze_text(tab, "#item-slices-hier\\.hy-page") == "unfreeze"
    assert row_state(tab, "#item-tasks-t1")["frozen"] is True
    assert row_state(tab, "#item-tasks-t3")["frozen"] is False


FREEZES = [freeze("plans/v2"), freeze("lane:ci", by="master@a-1"), freeze("plans/hier", verb="focus")]


def open_swarm(tab):
    tab.get_by_role("tab", name="Swarm", exact=False).click()
    tab.locator("#swarm-freezes tr").first.wait_for()


@pytest.mark.parametrize("page", [FREEZES], indirect=True)
def test_the_swarm_panel_lists_every_freeze_and_focus_with_its_button(page):
    tab, _ = page
    open_swarm(tab)
    rows = tab.evaluate(
        """() => [...document.querySelectorAll('#swarm-freezes tr')].map((tr) =>
          [...tr.querySelectorAll('td')].slice(1, 4).map((td) => td.textContent).concat(tr.querySelector('button').textContent))"""
    )
    assert rows == [
        ["freeze", "plan Swarm v2", "operator", "unfreeze"],
        ["freeze", "lane ci", "master@a-1", "unfreeze"],
        ["focus", "plan Plan hierarchy", "operator", "end focus"],
    ]
    assert tab.locator("#freeze-count").text_content() == "2 freezes · 1 focus"
    assert tab.locator("#swarm-freezes tr .snowflake").count() == 3


@pytest.mark.parametrize("page", [FREEZES], indirect=True)
def test_end_focus_in_the_swarm_panel_sends_the_clear(page):
    tab, sent = page
    open_swarm(tab)
    tab.locator("#swarm-freezes tr", has_text="focus").get_by_role("button", name="end focus").click()
    assert sent_ops(tab, sent, "freeze_clear") == [{"op": "freeze_clear", "target": "plans/hier"}]
    tab.wait_for_function("() => document.querySelectorAll('#swarm-freezes tr').length === 2")


def test_the_freezes_block_folds_and_remembers_it_after_a_reload(page):
    tab, _ = page
    open_swarm(tab)
    tab.locator("#freeze-fold > summary h3").click()
    tab.wait_for_function("() => Object.values(localStorage).some((v) => v.includes('\"freeze-fold\":false'))")
    tab.reload()
    loaded(tab)
    assert tab.eval_on_selector("#freeze-fold", "d => d.open") is False


def test_the_swarm_panel_says_when_nothing_is_frozen(page):
    tab, _ = page
    open_swarm(tab)
    assert tab.locator("#swarm-freezes tr").all_text_contents() == ["Nothing is frozen."]


def test_marking_a_plan_out_of_scope_sends_its_change(page):
    tab, sent = page
    tab.click("#item-plans-v2 > details.plan-fold > summary .scope")
    for _ in range(40):
        if sent["changes"]:
            break
        tab.wait_for_timeout(50)
    assert sent["changes"] == [{"path": "plans/v2/out_of_scope", "value": True, "base": False}]
    assert tab.evaluate("() => document.querySelector('#item-plans-v2').classList.contains('out')") is True


def test_the_snowflake_colour_is_a_palette_token_without_glow(page):
    tab, _ = page
    style = tab.evaluate(
        """() => {
          const flake = document.createElement('span');
          flake.className = 'snowflake';
          document.body.append(flake);
          const s = getComputedStyle(flake);
          const token = getComputedStyle(document.documentElement).getPropertyValue('--frozen').trim();
          return { token, color: s.color, shadow: s.textShadow, filter: s.filter };
        }"""
    )
    assert style["token"]
    assert (style["shadow"], style["filter"]) == ("none", "none")


@pytest.mark.parametrize("page", [[freeze("tasks/t1")]], indirect=True)
def test_freezing_a_group_lead_holds_its_members(page):
    tab, _ = page
    assert row_state(tab, "#item-tasks-t5")["frozen"] is True
    assert row_state(tab, "#item-tasks-t2")["frozen"] is False


@pytest.mark.parametrize("page", [[freeze("phases/p4")]], indirect=True)
def test_an_out_of_scope_item_keeps_its_freeze_button(page):
    tab, sent = page
    assert freeze_text(tab, "#item-phases-p4") == "unfreeze"
    tab.click("#item-phases-p4 .freeze")
    assert sent_ops(tab, sent, "freeze_clear") == [{"op": "freeze_clear", "target": "phases/p4"}]
    tab.wait_for_function("() => document.querySelector('#item-phases-p4 .freeze').textContent === 'freeze'")


@pytest.mark.parametrize("page", [[freeze("lane:ci", verb="focus")]], indirect=True)
def test_a_lane_focus_holds_tasks_outside_the_lane_and_no_phase(page):
    tab, _ = page
    assert row_state(tab, "#item-tasks-t1")["held"] is True
    assert row_state(tab, "#item-tasks-t3")["held"] is False
    for selector in ("#item-phases-p1", "#item-phases-p2"):
        assert row_state(tab, selector)["held"] is False
    assert tab.locator("#item-plans-v2 > .snowflake").count() == 0
