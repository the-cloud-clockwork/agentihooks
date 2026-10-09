import json
import re
from pathlib import Path

import pytest

from tests.swarm_ledger.ledger_page import (
    fulfill_events,
    is_events,
    ledger_state,
    loaded,
    page_source,
    serve_modules,
    shell_html,
)
from tests.swarm_ledger.test_caps_columns import browser as chromium_browser

browser = chromium_browser
ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "scripts" / "swarm_ledger" / "template.html"
URL = "http://ledger.test/layout"
NOW_MS = 1_791_300_000_000
NOW_S = NOW_MS // 1000
DOC = {
    "title": "Layout proof",
    "overview": "o",
    "phases": [{"id": f"p{i}", "title": f"Phase {i}", "description": "d", "done": i < 3} for i in range(1, 6)],
    "tasks": [{"id": "pb10", "title": "Build", "state": "claimed"}],
}
ROWS = [
    "swarm-head",
    "swarm-alert",
    "capacity-box",
    "swarm-row-work",
    "swarm-row-accounts",
    "health-box",
    "handoff-box",
]


def status(**changes):
    base = {
        "config": {
            "slug": "rig",
            "name": "swarm@a1b2c3",
            "state": "running",
            "max_eng": 3,
            "max_ci": 1,
            "max_plan": 1,
            "autonomy": "delegate",
            "effort_min": "medium",
            "effort_max": "high",
        },
        "last_tick": NOW_MS - 12_000,
        "compact_limit": 600,
        "done_today": 4,
        "tasks": {"open": 9, "claimed": 3, "pr": 1, "blocked": 1, "done": 2},
        "agents": [
            {
                "name": "master-1",
                "lane": "master",
                "harness": "claude",
                "model": "claude-opus-5-5",
                "effort": "high",
                "account": "luna",
                "started_at": NOW_MS - 7_200_000,
                "status": "working",
                "inbox": [{"text": "x"}],
            },
            {
                "name": "eng-58",
                "lane": "eng",
                "harness": "claude",
                "model": "claude-opus-5-5",
                "effort": "high",
                "task": "pb10",
                "started_at": NOW_MS - 60_000,
                "status": "idle",
                "inbox": [],
            },
        ],
        "findings": [],
        "quota": {
            "probed_at": NOW_S - 120,
            "rows": [
                {
                    "agent": "claude",
                    "account": "tccgma",
                    "five_hour_left": 92.4,
                    "five_hour_resets_at": NOW_S + 53 * 60,
                    "seven_day_left": 78,
                    "seven_day_resets_at": NOW_S + 4 * 86400 + 15 * 3600,
                    "sessions": 2,
                    "cap": 6,
                },
                {
                    "agent": "claude",
                    "account": "luna",
                    "five_hour_left": 64,
                    "five_hour_resets_at": NOW_S + 2 * 3600 + 5 * 60,
                    "seven_day_left": 51,
                    "seven_day_resets_at": NOW_S + 6 * 86400,
                    "sessions": 1,
                    "cap": 4,
                },
                {
                    "agent": "codex",
                    "account": "default",
                    "five_hour_left": None,
                    "five_hour_resets_at": None,
                    "seven_day_left": 61,
                    "seven_day_resets_at": NOW_S + 3 * 86400 + 10 * 3600,
                    "sessions": 0,
                },
            ],
        },
        "doctor": {"slug": "", "state": "not running", "last_check": 0, "findings": 0},
        "handoffs": [],
    }
    return {**base, **changes}


class Page:
    def __init__(self, browser, payload, width, clock=False, **options):
        self.payload, self.puts = payload, []
        html = shell_html()
        self.context = browser.new_context(viewport={"width": width, "height": 2400}, **options)
        if clock:
            self.context.clock.install(time=NOW_MS)
        self.context.add_init_script(f"Date.now = () => {NOW_MS};")
        self.context.route("**/*", lambda route: self.route(route, html))
        serve_modules(self.context)
        self.tab = self.context.new_page()
        self.errors = []
        self.tab.on("pageerror", lambda error: self.errors.append(str(error)))
        self.tab.goto(URL + "#swarm")
        loaded(self.tab)
        self.tab.locator("#swarm-agents tr").first.wait_for(timeout=3000)

    def route(self, route, html):
        request = route.request
        if is_events(request.url):
            fulfill_events(route, ledger_state(DOC), self.payload)
        elif "/api/swarm/" in request.url:
            if request.method == "PUT":
                self.puts.append(json.loads(request.post_data))
            route.fulfill(json=self.payload)
        elif request.url.startswith(URL):
            route.fulfill(body=html, content_type="text/html")
        else:
            route.abort()

    def text(self, selector):
        return self.tab.locator(selector).inner_text()

    def table(self, tbody):
        return self.tab.eval_on_selector_all(
            f"#{tbody} tr", "rows => rows.map(r => [...r.cells].map(c => c.innerText.trim()))"
        )

    def box(self, element_id):
        return self.tab.locator(f"#{element_id}").bounding_box()


@pytest.fixture
def open_page(browser):
    pages = []

    def make(payload=None, width=1440, clock=False, **options):
        page = Page(browser, payload or status(), width, clock, **options)
        pages.append(page)
        return page

    yield make
    for page in pages:
        assert page.errors == []
        page.context.close()


def test_rows_run_header_alert_capacity_work_accounts_health_handoffs():
    page = page_source()
    box = page.split('<div id="swarm-box">', 1)[1].split('<aside id="stats-column"', 1)[0]
    positions = [box.index(f'id="{row}"') for row in ROWS]
    assert positions == sorted(positions)
    assert 'class="fold sw-command-log" id="command-log"' in box
    assert '<details class="fold sw-fold" id="overlays-fold" open>' in box
    assert '<details class="fold sw-fold" id="quota-fold" open>' in box
    box = re.sub(r'<details[^>]*id="(command-log|overlays-fold|quota-fold)".*?</details>', "", box, flags=re.S)
    for gone in ("crew", "needs-you", "swarm-figs", "swarm-work", "restore-box", "<section", "<details"):
        assert gone not in box, gone


def test_column_pairs_sit_side_by_side_on_desktop_and_stack_on_a_phone(open_page):
    for width, side_by_side in ((1440, True), (390, False)):
        page = open_page(status(), width)
        for left, right in (("agents-box", "overlays-box"), ("quota-box", "doctor-box")):
            a, b = page.box(left), page.box(right)
            if side_by_side:
                assert abs(a["y"] - b["y"]) < 1 and a["x"] + a["width"] <= b["x"] + 1, (left, right)
            else:
                assert b["y"] >= a["y"] + a["height"] - 1, (left, right)
        full = page.box("swarm-box")["width"]
        for row in ("capacity-box", "health-box", "handoff-box"):
            assert abs(page.box(row)["width"] - full) < 1, row


def test_header_names_the_swarm_state_tick_controls_and_autonomy(open_page):
    page = open_page()
    assert page.text("#swarm-name") == "swarm@a1b2c3"
    assert page.text("#swarm-state").lower() == "running"
    assert page.text("#swarm-tick") == "tick 12s ago"
    controls = page.tab.eval_on_selector_all("#swarm-ctl button", "bs => bs.map(b => [b.textContent, b.disabled])")
    assert controls == [
        ["start", True],
        ["pause", False],
        ["stop", False],
        ["stop now", False],
        ["close ledger", False],
    ]
    modes = page.tab.eval_on_selector_all(
        "#swarm-modes button", "bs => bs.map(b => [b.textContent, b.getAttribute('aria-pressed')])"
    )
    assert modes == [["manual", "false"], ["assist", "false"], ["delegate", "true"], ["full", "false"]]
    page.tab.get_by_role("button", name="manual").click()
    page.tab.wait_for_function("() => !document.querySelector('#swarm-modes button').disabled")
    assert page.puts == [{"action": "set", "autonomy": "manual"}]


def test_alert_line_shows_only_while_a_finding_is_open_with_the_live_caps(open_page):
    assert not open_page().tab.locator("#swarm-alert").is_visible()
    finding = {
        "id": "idle-claim/eng-58",
        "kind": "idle claim",
        "subject": "eng-58",
        "evidence": ["3 idle ticks"],
        "threshold": "3 ticks",
        "seen_at": NOW_MS - 240_000,
        "verdict": None,
    }
    judged = {
        **finding,
        "id": "ceremony/x",
        "kind": "ceremony",
        "verdict": {"value": "false-positive", "note": "pr open"},
    }
    page = open_page(status(findings=[finding, judged]))
    assert page.tab.locator("#swarm-alert").is_visible()
    assert page.text("#alert-count").lower() == "1 finding open"
    assert page.text("#alert-kinds") == "idle claim"
    assert page.text("#alert-live").split() == ["eng", "1/3", "ci", "0/1", "plan", "0/1"]


def test_capacity_fields_take_typed_values_and_apply_sends_one_set(open_page):
    page = open_page()
    fields = {lane: page.tab.locator(f"#cap-{lane}") for lane in ("eng", "ci", "plan", "compact")}
    assert [f.input_value() for f in fields.values()] == ["3", "1", "1", "600"]
    apply = page.tab.get_by_role("button", name="Apply capacity")
    assert apply.is_disabled()
    fields["eng"].fill("5")
    fields["ci"].fill("2")
    fields["compact"].fill("700")
    page.tab.get_by_role("button", name="Raise compact limit").click()
    assert fields["compact"].input_value() == "750"
    page.tab.wait_for_timeout(2500)
    assert page.puts == []
    assert fields["eng"].input_value() == "5"
    apply.click()
    page.tab.wait_for_function("() => document.querySelector('#swarm-note').textContent.includes('pending')")
    assert page.puts == [{"action": "set", "max_eng": 5, "max_ci": 2, "compact_limit": 750}]
    assert apply.is_disabled()


def test_the_effort_range_shows_in_the_caps_row_and_apply_sends_it(open_page):
    page = open_page(width=1920)
    floor, ceiling = page.tab.locator("#cap-effort_min"), page.tab.locator("#cap-effort_max")
    assert (floor.input_value(), ceiling.input_value()) == ("medium", "high")
    assert floor.bounding_box()["y"] == page.tab.locator("#cap-eng").bounding_box()["y"]
    page.tab.get_by_role("button", name="Raise effort ceiling").click()
    assert page.tab.get_by_role("button", name="Raise effort ceiling").is_disabled()
    floor.fill("low")
    page.tab.get_by_role("button", name="Apply capacity").click()
    page.tab.wait_for_function("() => document.querySelector('#swarm-note').textContent.includes('pending')")
    assert page.puts == [{"action": "set", "effort_min": "low", "effort_max": "max"}]


def test_master_affinity_shows_the_live_harness_and_apply_sends_only_a_change(open_page):
    page = open_page(status(master_affinity={"desired": "auto", "live": "claude", "order": None}), width=1920)
    pick, apply = page.tab.locator("#cap-master_agent"), page.tab.get_by_role("button", name="Apply capacity")
    assert pick.input_value() == "claude" and apply.is_disabled()
    row = apply.bounding_box()
    assert row["y"] <= pick.bounding_box()["y"] + pick.bounding_box()["height"] / 2 <= row["y"] + row["height"]
    pick.select_option("claude")
    assert apply.is_disabled()
    pick.select_option("codex")
    assert not apply.is_disabled()
    apply.click()
    page.tab.wait_for_function("() => document.querySelector('#swarm-note').textContent.includes('pending')")
    assert page.puts == [{"action": "set", "master_agent": "codex"}]


@pytest.mark.parametrize(
    ("order", "text", "bad"),
    [
        (None, "", False),
        ({"to": "codex", "state": "ordered", "reason": ""}, "master hands off to codex", False),
        (
            {"to": "codex", "state": "failed", "reason": "no codex account"},
            "switch to codex failed: no codex account",
            True,
        ),
    ],
)
def test_master_affinity_shows_the_pending_or_failed_order(open_page, order, text, bad):
    page = open_page(status(master_affinity={"desired": "codex", "live": "claude", "order": order}))
    assert page.tab.locator("#cap-master_agent").input_value() == "codex"
    state = page.tab.locator("#affinity-state")
    assert state.text_content() == text
    assert ("bad" in state.get_attribute("class").split()) is bad


@pytest.mark.parametrize(
    ("lane", "typed", "message"),
    [
        ("eng", "51", "eng must be a whole number from 0 to 50"),
        ("ci", "1.5", "ci must be a whole number from 0 to 50"),
        ("compact", "90", "compact limit must be a whole number from 100 to 1000"),
    ],
)
def test_an_invalid_capacity_value_is_refused_before_sending(open_page, lane, typed, message):
    page = open_page()
    page.tab.locator("#cap-plan").fill("2")
    page.tab.locator(f"#cap-{lane}").fill(typed)
    assert page.tab.locator(f"#cap-{lane}").get_attribute("aria-invalid") == "true"
    page.tab.locator(f"#cap-{lane}").press("Enter")
    page.tab.wait_for_function("() => document.querySelector('#swarm-note').textContent.includes('Could not')")
    assert message in page.text("#swarm-note")
    assert page.puts == []


def test_capacity_caps_and_apply_share_one_row_and_gates_start_below(open_page):
    page = open_page(status(gate_modes={"talk": "observe", "watch": "enforce"}))
    page.tab.set_viewport_size({"width": 1920, "height": 1080})
    boxes = page.tab.evaluate(
        """() => {
          const box = (el) => el.getBoundingClientRect();
          return {
            row: [...document.querySelectorAll('#capacity-box .sw-caps > *')].map((el) => [box(el).top, box(el).bottom]),
            gates: box(document.getElementById('gates-label')).top,
          };
        }"""
    )
    assert len(boxes["row"]) == 8
    caps, (affinity, apply) = boxes["row"][:6], boxes["row"][6:]
    middle = (caps[0][0] + caps[0][1]) / 2
    assert all(top <= middle <= bottom for top, bottom in caps)
    last = (affinity[0] + affinity[1]) / 2
    assert apply[0] <= last <= apply[1]
    assert boxes["gates"] >= max(bottom for _, bottom in boxes["row"])


@pytest.mark.parametrize(("limit", "down", "up"), [(100, True, False), (1000, False, True), (600, False, False)])
def test_compact_limit_steps_stop_at_100_and_1000(open_page, limit, down, up):
    page = open_page(status(compact_limit=limit))
    state = page.tab.eval_on_selector_all("[data-swarm^=compact_]", "bs => bs.map(b => b.disabled)")
    assert state == [down, up]
    page.tab.locator("#cap-compact").fill("1000")
    assert page.tab.eval_on_selector_all("[data-swarm^=compact_]", "bs => bs.map(b => b.disabled)") == [False, True]


def test_agents_table_lists_the_master_first_with_overlays_and_stats_carry_the_task_figures(open_page):
    payload = status()
    payload["agents"][1]["overlays"] = ["qitp-tuner", "trader", "reviewer"]
    page = open_page(payload)
    assert page.table("swarm-agents") == [
        ["master-1", "—", "—", "—", "opus high", "—", "LIVE", "2h 0m", "message\nterminate"],
        [
            "eng-58",
            "eng",
            "—",
            "qitp-tuner · trader · reviewer",
            "opus high",
            "pb10",
            "IDLE",
            "1m",
            "message\nterminate",
        ],
    ]
    assert page.text("#agents-count").lower() == "2 live"
    for gone in ("swarm-tasks-box", "swarm-tasks", "tasks-open"):
        assert page.tab.locator(f"#{gone}").count() == 0, gone
    pairs = page.tab.eval_on_selector_all(
        "#stats .stat", "rows => rows.map(r => [r.children[0].innerText, r.children[1].innerText])"
    )
    figures = [[label.lower(), value] for label, value in pairs[4:12]]
    assert figures == [
        ["tasks", "0 / 1"],
        ["open", "9"],
        ["claimed", "3"],
        ["in pr", "1"],
        ["blocked", "1"],
        ["done today", "4"],
        ["next phase", "p3"],
        ["inbox pending", "1"],
    ]


def test_an_agents_table_without_agents_spans_all_nine_columns(open_page):
    page = open_page(status(agents=[]))
    assert page.tab.eval_on_selector("#swarm-agents td", "td => td.colSpan") == 9
    assert page.tab.eval_on_selector_all("#agents-table th", "ths => ths.length") == 9


def test_agents_table_lists_live_agents_first_and_pages_the_finished_ones_newest_first(open_page):
    payload = status()
    finished = [
        {
            "name": f"done-{i}",
            "lane": "eng",
            "task": f"t{i}",
            "state": "finished",
            "status": "finished",
            "started_at": NOW_MS - 9_000_000,
            "state_since": NOW_MS - 8_000_000 + i,
            "inbox": [],
        }
        for i in range(120)
    ]
    payload["agents"] = finished[:60] + payload["agents"] + finished[60:]
    page = open_page(payload, width=1920)
    rows = page.table("swarm-agents")
    assert [row[0] for row in rows[:2]] == ["master-1", "eng-58"]
    assert [row[0] for row in rows[2:52]] == [f"done-{i}" for i in range(119, 69, -1)]
    assert all(row[6] == "FINISHED" and row[8] == "" for row in rows[2:52])
    assert [cell.lower() for cell in rows[52]] == ["show 50 more finished agents · 70 left"]
    assert page.text("#agents-count").lower() == "2 live · 120 finished"
    page.tab.locator("#swarm-agents .page-more button").click()
    assert len(page.table("swarm-agents")) == 2 + 100 + 1


OFFERED = [
    {"name": "qitp-tuner", "wears": ["engineer", "qa"]},
    {"name": "trader", "wears": ["engineer"]},
    {"name": "reviewer", "wears": ["engineer", "planner"]},
    {"name": "sre", "wears": ["engineer", "cicd"]},
]


def overlay_rows(page):
    return page.tab.eval_on_selector_all(
        "#swarm-overlays .sw-ovl-row",
        """rows => rows.map(r => [r.querySelector('.sw-cap-name').innerText,
          [...r.querySelectorAll('button[data-overlay]')].map(b => [b.innerText, b.getAttribute('aria-pressed'), b.disabled]),
          (r.querySelector('.sw-ovl-none') || {}).innerText || ''])""",
    )


def test_overlays_box_lists_every_base_role_with_the_overlays_that_wear_it(open_page):
    payload = status(overlays_available=OFFERED)
    payload["config"]["overlays"] = {"engineer": ["trader"], "master": ["gone"]}
    page = open_page(payload)
    assert page.text("#overlays-count").lower() == "4 offered · 3 max"
    none = "no overlay in the bundle wears this role"
    assert overlay_rows(page) == [
        ["master", [["gone", "true", False]], ""],
        [
            "engineer",
            [
                ["qitp-tuner", "false", False],
                ["trader", "true", False],
                ["reviewer", "false", False],
                ["sre", "false", False],
            ],
            "",
        ],
        ["planner", [["reviewer", "false", False]], ""],
        ["qa", [["qitp-tuner", "false", False]], ""],
        ["cicd", [["sre", "false", False]], ""],
    ]
    assert page.tab.locator("#overlays-apply").is_disabled()
    payload = status(overlays_available=[])
    assert [row[2] for row in overlay_rows(open_page(payload))] == [none] * 5


def test_a_role_takes_at_most_three_overlays_and_apply_sends_only_changed_roles(open_page):
    payload = status(overlays_available=OFFERED)
    payload["config"]["overlays"] = {"planner": ["reviewer"]}
    page = open_page(payload)
    row = page.tab.locator(".sw-ovl-row", has_text="engineer")
    for name in ("qitp-tuner", "trader", "reviewer"):
        row.locator(f'button[data-overlay="{name}"]').click()
    assert overlay_rows(page)[1][1] == [
        ["qitp-tuner", "true", False],
        ["trader", "true", False],
        ["reviewer", "true", False],
        ["sre", "false", True],
    ]
    row.locator('button[data-overlay="trader"]').click()
    row.locator('button[data-overlay="sre"]').click()
    page.tab.locator(
        '.sw-ovl-row[aria-label="Overlays the planner role wears"] button[data-overlay="reviewer"]'
    ).click()
    page.tab.locator("#overlays-apply").click()
    page.tab.wait_for_function("() => document.querySelector('#overlays-apply').disabled")
    assert page.puts == [{"action": "set", "overlays": {"engineer": ["qitp-tuner", "reviewer", "sre"], "planner": []}}]


def test_toggling_an_overlay_back_leaves_nothing_to_apply(open_page):
    page = open_page(status(overlays_available=OFFERED))
    button = page.tab.locator('.sw-ovl-row[aria-label="Overlays the qa role wears"] button[data-overlay="qitp-tuner"]')
    button.click()
    assert not page.tab.locator("#overlays-apply").is_disabled()
    button.click()
    assert page.tab.locator("#overlays-apply").is_disabled()


def test_the_overlays_box_folds_on_its_header_and_remembers_it(open_page):
    page = open_page(status(overlays_available=OFFERED))
    assert page.tab.locator("#swarm-overlays").is_visible()
    page.tab.locator("#overlays-fold > summary").click()
    assert not page.tab.locator("#swarm-overlays").is_visible()
    page.tab.wait_for_function("() => Object.values(localStorage).some((v) => v.includes('\"overlays-fold\":false'))")
    page.tab.reload()
    page.tab.locator("#swarm-agents tr").first.wait_for(timeout=3000)
    assert page.tab.eval_on_selector("#overlays-fold", "d => d.open") is False


@pytest.mark.parametrize("width", [1440, 390])
def test_full_length_agent_names_stay_on_one_line_and_fit_the_desktop_column(open_page, width):
    payload = status()
    for agent, name in zip(payload["agents"], ["master@323133-0004", "engineer@323133-0072"]):
        agent["name"] = name
        agent["profile"] = "engineer"
    page = open_page(payload, width)
    if width == 1440:
        fit = page.tab.eval_on_selector("#agents-box .sw-scroll", "el => [el.scrollWidth, el.clientWidth]")
        assert fit[0] <= fit[1], fit
    lines = page.tab.eval_on_selector_all(
        "#swarm-agents td",
        """tds => tds.filter(td => td.innerText.trim()).map(td => {
          const tops = new Set(), walk = document.createTreeWalker(td, NodeFilter.SHOW_TEXT);
          for (let n = walk.nextNode(); n; n = walk.nextNode()) {
            const r = document.createRange();
            r.selectNodeContents(n);
            for (const box of r.getClientRects()) tops.add(Math.round(box.top));
          }
          return [td.innerText, tops.size];
        })""",
    )
    assert lines and all(count == 1 for _text, count in lines if "\n" not in _text), lines
    assert [row[0] for row in page.table("swarm-agents")] == ["master@323133-0004", "engineer@323133-0072"]
    titles = page.tab.eval_on_selector_all("#swarm-agents td:first-child .sw-id", "ids => ids.map(e => e.title)")
    assert titles == ["master@323133-0004", "engineer@323133-0072"]


def test_every_button_is_flat_at_rest(open_page):
    page = open_page()
    page.tab.mouse.move(1439, 2399)
    rest = page.tab.evaluate(
        """() => [...document.querySelectorAll("button:not([aria-selected=true])")].filter(b => b.getClientRects().length).map(b => {
          const s = getComputedStyle(b);
          return [b.className || b.id, s.backgroundColor, s.backgroundImage, s.boxShadow, s.backdropFilter,
            ["Top", "Right", "Bottom", "Left"].every(side => s[`border${side}Width`] === "0px" || s[`border${side}Color`] === "rgba(0, 0, 0, 0)")];
        })"""
    )
    assert len(rest) > 20
    for name, color, image, shadow, glass, frameless in rest:
        assert (color, image, shadow, glass, frameless) == ("rgba(0, 0, 0, 0)", "none", "none", "none", True), (
            name,
            color,
            image,
            shadow,
            glass,
            frameless,
        )


def test_quota_rows_come_from_the_stubbed_balance_and_mark_the_master_account(open_page):
    page = open_page()
    assert page.table("swarm-quota") == [
        ["tccgma", "claude", "SUBSCRIPTION", "—", "92%", "53m", "78%", "4d15h", "—", "2", "—", "6", ""],
        ["luna", "claude", "SUBSCRIPTION", "—", "64%", "2h05m", "51%", "6d00h", "—", "1", "—", "4", "MASTER"],
        ["default", "codex", "SUBSCRIPTION", "—", "—", "—", "61%", "3d10h", "—", "0", "—", "—", ""],
    ]
    assert page.text("#quota-count").lower() == "3 accounts · probed 2m ago"


def test_the_quota_table_with_its_capacity_line_fits_its_box_at_1920(open_page):
    payload = status()
    payload["quota_capacity"] = {
        "configured": {"eng": 4, "ci": 1, "plan": 1},
        "effective": {"eng": 2, "ci": 1, "plan": 0},
        "reason": "accounts are closed; Claude has 1 free seats and Codex has 1 free seats",
        "lanes": ["eng", "ci", "plan"],
        "accounts": [
            {
                "harness": "claude",
                "name": "tccgma",
                "state": "CLOSED",
                "sessions": 2,
                "five_left": 92,
                "week_left": 78,
                "routing": 78,
            }
        ],
        "at": NOW_MS - 7 * 60_000,
    }
    page = open_page(payload, 1920)
    scroll = page.tab.eval_on_selector("#quota-box .sw-scroll", "s => [s.scrollWidth, s.clientWidth]")
    assert scroll[0] == scroll[1]
    assert page.table("swarm-quota")[0][3] == "CLOSED"
    assert page.text("#quota-capacity").split("\n") == [
        "ENG 2 OF 4 · CI 1 OF 1 · PLAN 0 OF 1",
        "changed 7m ago",
        "because accounts are closed; Claude has 1 free seats and Codex has 1 free seats",
    ]
    table, line = page.box("quota-table"), page.box("quota-capacity")
    assert abs(table["x"] - line["x"]) < 1


def test_the_sessions_cell_reads_the_plain_session_count(open_page):
    page = open_page()
    parts = page.tab.eval_on_selector_all(
        "#swarm-quota tr:first-child .sw-sessions-value",
        "els => els.map(e => [e.tagName, e.innerText])",
    )
    assert parts == [["SPAN", "2"]]


def test_every_capacity_stepper_puts_minus_before_and_plus_after_its_value(open_page):
    page = open_page()
    order = page.tab.eval_on_selector_all(
        "#capacity-box .sw-cap:not(.sw-affinity)",
        """caps => caps.map(c => [c.querySelector('.sw-cap-name').innerText,
          [...c.children].slice(1).map(e => e.matches('.sw-field') ? e.querySelector('input').value + (e.querySelector('.sw-unit')?.innerText || '') : e.innerText)])""",
    )
    assert order == [
        ["eng", ["−", "3", "+"]],
        ["ci", ["−", "1", "+"]],
        ["plan", ["−", "1", "+"]],
        ["compact limit", ["−", "600k", "+"]],
        ["effort min", ["−", "medium", "+"]],
        ["effort max", ["−", "high", "+"]],
    ]


def test_the_refresh_icon_beside_the_title_probes_every_account_and_redraws(open_page):
    page = open_page()
    button = page.tab.locator("#quota-box .sw-blockhead #quota-refresh")
    assert button.get_attribute("aria-label") == "Refresh quota"
    assert "sw-btn" in button.get_attribute("class")
    look = button.evaluate(
        """b => { const s = getComputedStyle(b), probe = document.createElement("i");
          probe.style.color = "var(--accent)"; document.body.append(probe);
          const accent = getComputedStyle(probe).color; probe.remove();
          return [s.color === accent, s.backgroundColor, s.borderStyle]; }"""
    )
    assert look == [True, "rgba(0, 0, 0, 0)", "none"]
    fresh = status()
    fresh["quota"] = {**fresh["quota"], "probed_at": NOW_S}
    page.payload = fresh
    button.click()
    page.tab.wait_for_function("() => document.querySelector('#quota-count').textContent.includes('probed 0s ago')")
    assert page.puts == [{"action": "quota_refresh"}]


def test_an_untouched_page_probes_quota_every_five_minutes(open_page):
    page = open_page(clock=True)
    page.tab.clock.run_for(299_000)
    page.tab.wait_for_timeout(200)
    assert {"action": "quota_refresh"} not in page.puts
    page.tab.clock.run_for(2_000)
    for _ in range(50):
        if {"action": "quota_refresh"} in page.puts:
            break
        page.tab.wait_for_timeout(50)
    assert page.puts.count({"action": "quota_refresh"}) == 1


def test_doctor_block_offers_start_while_off_and_links_its_ledger_while_on(open_page):
    off = open_page()
    assert off.text("#doctor-state").lower() == "off"
    assert [r.split("\n") for r in off.tab.locator("#swarm-doctor .kv").all_inner_texts()] == [
        ["state", "not running"],
        ["last check", "—"],
        ["findings", "—"],
    ]
    assert off.tab.locator("[data-swarm=doctor_start]").is_visible()
    assert not off.tab.locator("[data-swarm=doctor_stop]").is_visible()
    on = open_page(
        status(doctor={"slug": "rig-doctor", "state": "running", "last_check": NOW_MS - 120_000, "findings": 2})
    )
    assert on.text("#doctor-state").lower() == "on"
    assert on.tab.locator("#swarm-doctor a").get_attribute("href") == "/rig-doctor"
    assert [r.split("\n")[1] for r in on.tab.locator("#swarm-doctor .kv").all_inner_texts()] == [
        "running",
        "2m ago",
        "2",
    ]
    assert on.tab.locator("[data-swarm=doctor_stop]").is_visible()


def test_health_rows_carry_evidence_threshold_seen_verdict_and_note(open_page):
    finding = {
        "id": "ceremony/eng-59",
        "kind": "ceremony",
        "subject": "eng-59",
        "evidence": ["24 moves", "1 outcome"],
        "threshold": "20 · ratio 12",
        "seen_at": NOW_MS - 540_000,
        "verdict": {"value": "false-positive", "note": "pr open"},
    }
    page = open_page(status(findings=[finding]))
    row = page.table("health")[0]
    assert row[:5] == ["ceremony", "eng-59", "24 moves · 1 outcome", "20 · ratio 12", "9m ago"]
    assert page.tab.locator("#health .hl-pick").input_value() == "false-positive"
    assert page.tab.locator("#health .hl-note").input_value() == "pr open"
    page.tab.locator("#health .hl-note").fill("still open")
    page.tab.locator("#health .hl-pick").select_option("resolved")
    page.tab.wait_for_function("() => !document.querySelector('#swarm-modes button').disabled")
    assert page.puts == [{"action": "verdict", "id": "ceremony/eng-59", "verdict": "resolved", "note": "still open"}]


def test_handoff_rows_show_each_seat_and_offer_the_restore_decision(open_page):
    handoffs = [
        {
            "seat": "master@rig",
            "at": NOW_MS,
            "reason": "recycle",
            "continuity": "confirmed",
            "binding": "live",
            "successor": "master-2",
            "awaiting": "",
        },
        {
            "seat": "eng-3@rig",
            "at": 0,
            "reason": "",
            "continuity": "",
            "binding": "awaiting decision",
            "successor": "eng-7",
            "awaiting": "eng-7",
        },
    ]
    page = open_page(status(handoffs=handoffs))
    rows = page.table("swarm-handoffs")
    assert rows[0][0] == "master@rig" and rows[0][2:] == ["recycle", "CONFIRMED", "LIVE", "master-2"]
    assert re.fullmatch(r"\d\d:\d\d", rows[0][1])
    assert rows[1][:5] == ["eng 3", "—", "—", "—", "AWAITING DECISION"]
    assert page.text("#handoff-count").lower() == "2 seats"
    page.tab.locator("#swarm-handoffs button", has_text="fresh").click()
    page.tab.wait_for_function("() => !document.querySelector('#swarm-modes button').disabled")
    assert page.puts == [{"action": "restore-decision", "agent": "eng-7", "choice": "fresh"}]


def test_swarm_tab_styles_use_only_palette_tokens():
    page = page_source()
    rules = re.findall(r"^\s*(?:\.sw-|#swarm|\.lbl|#health-table|#agents-table|#quota-table)[^{]*\{[^}]*\}", page, re.M)
    assert len(rules) > 30
    for rule in rules:
        assert not re.search(
            r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(|(?<![-\w])(white|black)(?![-\w])", rule.split("{", 1)[1]
        ), rule
