import json
import re
from pathlib import Path

import pytest

from tests.swarm_ledger.test_caps_columns import browser as chromium_browser

browser = chromium_browser
ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "scripts" / "swarm_ledger" / "template.html"
URL = "http://ledger.test/layout"
NOW_MS = 1_791_300_000_000
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
            "state": "running",
            "max_eng": 3,
            "max_ci": 1,
            "max_plan": 1,
            "codex_share": 20,
            "autonomy": "delegate",
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
            "cap": 3,
            "rows": [
                {"agent": "claude", "account": "tccgma", "five_hour_left": 92.4, "seven_day_left": 78, "sessions": 2},
                {"agent": "claude", "account": "luna", "five_hour_left": 64, "seven_day_left": 51, "sessions": 1},
                {"agent": "codex", "account": "default", "five_hour_left": None, "seven_day_left": 61, "sessions": 0},
            ],
        },
        "doctor": {"slug": "", "state": "not running", "last_check": 0, "findings": 0},
        "handoffs": [],
    }
    return {**base, **changes}


class Page:
    def __init__(self, browser, payload, width, **options):
        self.payload, self.puts = payload, []
        html = TEMPLATE.read_text().replace("__LEDGER_DATA__", json.dumps(DOC))
        html = html.replace("__LEDGER_PALETTE__", (ROOT / "scripts/swarm_ledger/palette.css").read_text())
        self.context = browser.new_context(viewport={"width": width, "height": 2400}, **options)
        self.context.add_init_script(f"Date.now = () => {NOW_MS};")
        self.context.route("**/*", lambda route: self.route(route, html))
        self.tab = self.context.new_page()
        self.errors = []
        self.tab.on("pageerror", lambda error: self.errors.append(str(error)))
        self.tab.goto(URL + "#swarm")
        self.tab.locator("#swarm-agents tr").first.wait_for(timeout=3000)

    def route(self, route, html):
        request = route.request
        if "/api/swarm/" in request.url:
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

    def make(payload=None, width=1440, **options):
        page = Page(browser, payload or status(), width, **options)
        pages.append(page)
        return page

    yield make
    for page in pages:
        assert page.errors == []
        page.context.close()


def test_rows_run_header_alert_capacity_work_accounts_health_handoffs():
    page = TEMPLATE.read_text()
    box = page.split('<div id="swarm-box">', 1)[1].split('<aside id="stats-column"', 1)[0]
    positions = [box.index(f'id="{row}"') for row in ROWS]
    assert positions == sorted(positions)
    for gone in ("crew", "needs-you", "swarm-figs", "swarm-work", "restore-box", "<section", "<details"):
        assert gone not in box, gone


def test_column_pairs_sit_side_by_side_on_desktop_and_stack_on_a_phone(open_page):
    for width, side_by_side in ((1440, True), (390, False)):
        page = open_page(status(), width)
        for left, right in (("agents-box", "swarm-tasks-box"), ("quota-box", "doctor-box")):
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
    assert page.text("#swarm-name") == "rig"
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
    fields = {lane: page.tab.locator(f"#cap-{lane}") for lane in ("eng", "ci", "plan", "codex", "compact")}
    assert [f.input_value() for f in fields.values()] == ["3", "1", "1", "20", "600"]
    apply = page.tab.get_by_role("button", name="Apply capacity")
    assert apply.is_disabled()
    fields["eng"].fill("5")
    fields["ci"].fill("2")
    fields["compact"].fill("700")
    page.tab.get_by_role("button", name="Raise compact limit").click()
    page.tab.get_by_role("button", name="Lower codex share").click()
    assert fields["compact"].input_value() == "750"
    assert fields["codex"].input_value() == "15"
    page.tab.wait_for_timeout(2500)
    assert page.puts == []
    assert fields["eng"].input_value() == "5"
    apply.click()
    page.tab.wait_for_function("() => document.querySelector('#swarm-note').textContent.includes('done')")
    assert page.puts == [{"action": "set", "max_eng": 5, "max_ci": 2, "codex_share": 15, "compact_limit": 750}]
    assert apply.is_disabled()


@pytest.mark.parametrize(
    ("lane", "typed", "message"),
    [
        ("eng", "51", "eng must be a whole number from 0 to 50"),
        ("ci", "1.5", "ci must be a whole number from 0 to 50"),
        ("codex", "abc", "codex share must be a whole number from 0 to 100"),
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
    page.tab.set_viewport_size({"width": 1440, "height": 900})
    boxes = page.tab.evaluate(
        """() => {
          const box = (el) => el.getBoundingClientRect();
          return {
            row: [...document.querySelectorAll('#capacity-box .sw-caps > *')].map((el) => [box(el).top, box(el).bottom]),
            gates: box(document.getElementById('gates-label')).top,
          };
        }"""
    )
    assert len(boxes["row"]) == 6
    middle = (boxes["row"][0][0] + boxes["row"][0][1]) / 2
    assert all(top <= middle <= bottom for top, bottom in boxes["row"])
    assert boxes["gates"] >= max(bottom for _, bottom in boxes["row"])


@pytest.mark.parametrize(("limit", "down", "up"), [(100, True, False), (1000, False, True), (600, False, False)])
def test_compact_limit_steps_stop_at_100_and_1000(open_page, limit, down, up):
    page = open_page(status(compact_limit=limit))
    state = page.tab.eval_on_selector_all("[data-swarm^=compact_]", "bs => bs.map(b => b.disabled)")
    assert state == [down, up]
    page.tab.locator("#cap-compact").fill("1000")
    assert page.tab.eval_on_selector_all("[data-swarm^=compact_]", "bs => bs.map(b => b.disabled)") == [False, True]


def test_agents_table_lists_the_master_first_and_tasks_block_counts(open_page):
    page = open_page()
    assert page.table("swarm-agents") == [
        ["master-1", "—", "—", "opus high", "—", "LIVE", "2h 0m", "message\nterminate"],
        ["eng-58", "eng", "—", "opus high", "pb10", "IDLE", "1m", "message\nterminate"],
    ]
    assert page.text("#agents-count").lower() == "2 live"
    assert page.text("#tasks-open").lower() == "9 open"
    pairs = page.tab.eval_on_selector_all(
        "#swarm-tasks .kv", "rows => rows.map(r => [r.children[0].innerText, r.children[1].innerText])"
    )
    assert pairs == [
        ["open", "9"],
        ["claimed", "3"],
        ["in pr", "1"],
        ["blocked", "1"],
        ["done today", "4"],
        ["phases", "2 / 5"],
        ["next phase", "p3"],
        ["inbox pending", "1"],
    ]


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
        ["tccgma", "claude", "92%", "78%", "2/3", ""],
        ["luna", "claude", "64%", "51%", "1/3", "HERE"],
        ["default", "codex", "—", "61%", "0/3", ""],
    ]
    assert page.text("#quota-count").lower() == "3 accounts"


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
    page = TEMPLATE.read_text()
    rules = re.findall(r"^\s*(?:\.sw-|#swarm|\.lbl|#health-table|#agents-table|#quota-table)[^{]*\{[^}]*\}", page, re.M)
    assert len(rules) > 30
    for rule in rules:
        assert not re.search(
            r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(|(?<![-\w])(white|black)(?![-\w])", rule.split("{", 1)[1]
        ), rule
