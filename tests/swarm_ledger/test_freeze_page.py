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
    param = getattr(request, "param", [])
    setup = param if isinstance(param, dict) else {"freezes": param}
    context = browser.new_context(viewport={"width": 1920, "height": 1080})
    from scripts.swarm_ledger import ledger_server

    html = shell_html().replace("__LEDGER_PAGE__", ledger_server.served_version())
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
    view = ledger_server.ledger_view(ledger_state({**DOC, **setup.get("doc", {}), "freezes": setup["freezes"]}))
    if setup.get("bare"):
        view["tasks"] = [{k: v for k, v in row.items() if k != "freeze_ancestry"} for row in view["tasks"]]
    serve_modules(context, view, SWARM)
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
    assert tab.locator("#item-plans-v2 > details.plan-fold > summary > .snowflake").count() == 1
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


@pytest.mark.parametrize("page", [[freeze("phases/p1"), freeze("tasks/t1")]], indirect=True)
def test_unfreezing_a_phase_keeps_the_freezes_on_its_tasks_on_the_page(page):
    tab, sent = page
    tab.click('[data-focus="freeze:phases/p1"]')
    assert sent_ops(tab, sent, "freeze_clear") == [{"op": "freeze_clear", "target": "phases/p1"}]
    tab.wait_for_function("() => !document.querySelector('#item-phases-p1').classList.contains('frozen')")
    assert row_state(tab, "#item-tasks-t1")["frozen"] is True
    assert freeze_text(tab, "#item-tasks-t1") == "unfreeze"
    assert row_state(tab, "#item-tasks-t3")["frozen"] is False


@pytest.mark.parametrize("page", [[freeze("plans/hier", verb="focus")]], indirect=True)
def test_a_focus_puts_the_snowflake_on_items_outside_it_only(page):
    tab, _ = page
    outside = row_state(tab, "#item-phases-p2")
    assert (outside["held"], outside["flake"], outside["frozen"], outside["checkbox"]) == (True, True, False, [False])
    assert row_state(tab, "#item-tasks-t2")["held"] is True
    assert tab.locator("#item-plans-v2 > details.plan-fold > summary > .snowflake").count() == 1
    for selector in ("#item-phases-p1", "#item-tasks-t1", "#item-tasks-t3"):
        state = row_state(tab, selector)
        assert (state["held"], state["flake"]) == (False, False)
    assert tab.locator("#item-plans-hier > details.plan-fold > summary > .snowflake").count() == 0


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


def test_the_freezes_block_folds_on_its_header_and_remembers_it(page):
    tab, _ = page
    open_swarm(tab)
    assert tab.locator("#freeze-table").is_visible()
    tab.locator("#freeze-fold > summary h3").click()
    assert tab.eval_on_selector("#freeze-fold", "d => d.open") is False
    tab.wait_for_function("() => Object.values(localStorage).some((v) => v.includes('\"freeze-fold\":false'))")


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
    state = row_state(tab, "#item-tasks-t5")
    assert (state["frozen"], state["rank"]) == (True, [True])
    assert row_state(tab, "#item-tasks-t2")["frozen"] is False


def test_page_snapshot_carries_the_shared_group_freeze_ancestry():
    from scripts.swarm_ledger import ledger_server

    snapshot = ledger_server.ledger_view(ledger_state(DOC))
    member = next(row for row in snapshot["tasks"] if row["id"] == "t5")
    assert set(member["freeze_ancestry"]) == {
        "tasks/t5",
        "tasks/t1",
        "slices/hier.hy-page",
        "phases/p1",
        "plans/hier",
        "phases/p3",
        "plans/v2",
    }
    assert "freeze_ancestry" not in DOC["tasks"][-1]


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
    assert tab.locator("#item-plans-v2 > details.plan-fold > summary > .snowflake").count() == 0


@pytest.mark.parametrize("page", [{"freezes": [freeze("phases/p1")], "bare": True}], indirect=True)
def test_a_folded_task_without_stored_ancestry_follows_its_merged_target(page):
    tab, _ = page
    assert row_state(tab, "#item-tasks-t5")["frozen"] is True
    assert row_state(tab, "#item-tasks-t2")["frozen"] is False


MERGE_CYCLE = {
    "tasks": [*DOC["tasks"], {**task("t6", "p2"), "merged_into": "t7"}, {**task("t7", "p3"), "merged_into": "t6"}]
}


@pytest.mark.parametrize("page", [{"freezes": [freeze("phases/p3")], "bare": True, "doc": MERGE_CYCLE}], indirect=True)
def test_folded_tasks_merged_into_each_other_render_and_share_their_freezes(page):
    tab, _ = page
    assert row_state(tab, "#item-tasks-t6")["frozen"] is True
    assert row_state(tab, "#item-tasks-t7")["frozen"] is True
    assert row_state(tab, "#item-tasks-t2")["frozen"] is False


BAD_PHASE = {
    "phases": [*DOC["phases"], {**phase("p9", "plans/v2", "Broken"), "depends_on": "p1"}],
    "followups": [{"id": "u1", "text": "Later", "done": False, "comments": []}],
}


@pytest.mark.parametrize("page", [{"freezes": [], "doc": BAD_PHASE}], indirect=True)
def test_one_malformed_item_leaves_the_other_sections_rendered(page):
    tab, _ = page
    assert tab.locator("#item-tasks-t2").count() == 1
    assert tab.locator("#item-followups-u1").count() == 1


LOOKUPS = """async () => {
  const main = document.querySelector("script[type=module][src$='/js/main.js']").src;
  const { frozen, held } = await import(new URL("freezes.js", main));
  const phases = Array.from({ length: 20 }, (_, i) => ({ id: `p${i}`, plan: `plans/v${i % 4}` }));
  const slices = phases.map((p) => ({ id: `s${p.id}`, phase: `phases/${p.id}` }));
  const raw = Array.from({ length: 400 }, (_, i) => ({ id: `t${i}`, phase: `p${i % 20}`, slice: `slices/sp${i % 20}`, lane: "eng" }));
  let reads = 0;
  const tasks = new Proxy(raw, { get(target, key, receiver) {
    if (typeof key === "string" && /^\\d+$/.test(key)) reads++;
    return Reflect.get(target, key, receiver);
  } });
  const freezes = [...Array.from({ length: 20 }, (_, i) => ({ verb: "freeze", target: `tasks/t${200 + i * 7}` })),
    { verb: "freeze", target: "phases/p3" }, { verb: "focus", target: "plans/v1" }];
  const d = { phases, slices, tasks, freezes };
  const pick = (test) => raw.filter((t) => test(d, `tasks/${t.id}`)).map((t) => t.id);
  return { frozen: pick(frozen), held: pick(held), reads };
}"""


@pytest.mark.parametrize("page", [[]], indirect=True)
def test_freeze_checks_read_each_task_a_few_times_however_many_rules_apply(page):
    tab, _ = page
    found = tab.evaluate(LOOKUPS)
    frozen = sorted({f"t{i}" for i in range(400) if i % 20 == 3} | {f"t{200 + i * 7}" for i in range(20)})
    held = sorted(f"t{i}" for i in range(400) if i % 20 % 4 != 1 and f"t{i}" not in frozen)
    assert sorted(found["frozen"]) == frozen
    assert sorted(found["held"]) == held
    assert found["reads"] <= 2721


REPLACED = """async () => {
  const main = document.querySelector("script[type=module][src$='/js/main.js']").src;
  const { frozen } = await import(new URL("freezes.js", main));
  const d = { phases: [{ id: "p1" }, { id: "p2" }], slices: [], freezes: [{ verb: "freeze", target: "phases/p2" }],
    tasks: [{ id: "t1", phase: "p1" }, { id: "t2", phase: "p1" }] };
  const before = frozen(d, "tasks/t2");
  d.tasks.splice(0, 1);
  d.tasks.push({ id: "t3", phase: "p2" });
  const moved = [before, frozen(d, "tasks/t3"), frozen(d, "tasks/t2")];
  d.tasks.splice(0, 1);
  d.tasks.push({ id: "t2", phase: "p2" });
  const readded = frozen(d, "tasks/t2");
  d.tasks.splice(1, 1);
  return [...moved, readded, frozen(d, "tasks/t2")];
}"""


@pytest.mark.parametrize("page", [[]], indirect=True)
def test_freeze_checks_find_a_task_added_after_another_left_the_list(page):
    tab, _ = page
    assert tab.evaluate(REPLACED) == [False, True, False, True, False]


@pytest.mark.parametrize("page", [[]], indirect=True)
def test_the_page_marks_its_first_full_render_once(page):
    tab, _ = page
    marks = tab.evaluate("() => performance.getEntriesByName('ledger-rendered').map((m) => m.entryType)")
    assert marks == ["mark"]
