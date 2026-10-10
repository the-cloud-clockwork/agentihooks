import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from tests.swarm_ledger.ledger_page import loaded
from tests.swarm_ledger.test_caps_columns import browser as chromium_browser
from tests.swarm_ledger.test_swarm_layout import NOW_MS, status

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"))
import ledger_core as core  # noqa: E402
import ledger_layout as layout  # noqa: E402
import ledger_server as server  # noqa: E402
import new_ledger  # noqa: E402

from tests.swarm_ledger import legacy_page  # noqa: E402

browser = chromium_browser
SLUGS = ("resize-one-2026-10-06", "resize-two-2026-10-06")
HEIGHT_ROWS = ("capacity-box", "swarm-row-work", "swarm-row-accounts", "health-box", "handoff-box")
PAIRS = {"swarm-row-work": ("agents-box", "overlays-box"), "swarm-row-accounts": ("quota-box", "doctor-box")}
FINDINGS = [
    {
        "id": f"idle-claim/eng-{n}",
        "kind": "idle claim",
        "subject": f"eng-{n}",
        "evidence": ["3 idle ticks"],
        "threshold": "3 ticks",
        "seen_at": NOW_MS - 60_000,
        "verdict": None,
    }
    for n in range(12)
]


@pytest.fixture(scope="module")
def base(ledger_dir):
    for slug in SLUGS:
        content = {"title": slug, "overview": "o", "sources": [], "phases": [], "questions": [], "followups": []}
        html_path, _ = core.paths(slug)
        html_path.write_text(legacy_page.render(new_ledger.build_doc(content), slug, 8765), encoding="utf-8")
        core.sync(slug)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    port = httpd.server_address[1]
    hosts = {f"127.0.0.1:{port}"}
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(server, "ALLOWED_HOSTS", hosts)
        patch.setattr(server, "ALLOWED_ORIGINS", {f"http://{host}" for host in hosts})
        patch.setattr(server, "swarm_status", lambda slug, state=None: status(findings=FINDINGS))
        threading.Thread(target=httpd.serve_forever, args=(0.01,), daemon=True).start()
        yield f"http://127.0.0.1:{port}"
        httpd.shutdown()
        httpd.server_close()


class Tab:
    def __init__(self, context, base, slug=SLUGS[0]):
        self.base, self.errors = base, []
        self.tab = context.new_page()
        self.tab.on("pageerror", lambda error: self.errors.append(str(error)))
        self.open(slug)

    def open(self, slug):
        self.tab.goto(f"{self.base}/{slug}#swarm")
        loaded(self.tab)
        self.tab.locator("#swarm-overlays .sw-ovl-row").first.wait_for(timeout=5000)
        self.tab.wait_for_function("() => document.documentElement.dataset.layout === 'ready'", timeout=5000)

    def reload(self):
        self.tab.reload()
        loaded(self.tab)
        self.tab.locator("#swarm-overlays .sw-ovl-row").first.wait_for(timeout=5000)
        self.tab.wait_for_function("() => document.documentElement.dataset.layout === 'ready'", timeout=5000)

    def box(self, element_id):
        return self.tab.locator(f"#{element_id}").bounding_box()

    def grip(self, kind, row):
        return self.tab.locator(f"[data-grip={kind}][data-row={row}]")

    def drag(self, kind, row, dx=0, dy=0):
        handle = self.grip(kind, row)
        handle.scroll_into_view_if_needed()
        box = handle.bounding_box()
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        self.tab.mouse.move(x, y)
        self.tab.mouse.down()
        self.tab.mouse.move(x + dx / 2, y + dy / 2)
        self.tab.mouse.move(x + dx, y + dy)
        self.tab.mouse.up()

    def split_to(self, row, percent):
        box, grip = self.box(row), self.grip("split", row).bounding_box()
        self.drag("split", row, dx=box["x"] + box["width"] * percent / 100 - (grip["x"] + grip["width"] / 2))

    def heights(self):
        return {row: round(self.box(row)["height"]) for row in HEIGHT_ROWS}

    def share(self, row):
        left = self.box(PAIRS[row][0])
        return 100 * left["width"] / self.box(row)["width"]

    def stored(self):
        return self.tab.evaluate("() => JSON.parse(localStorage.getItem('plan-ledger:swarm-layout') || 'null')")


@pytest.fixture
def visit(browser, base):
    layout.path().unlink(missing_ok=True)
    contexts, tabs = [], []

    def make(slug=SLUGS[0], width=1440, context=None):
        if context is None:
            context = browser.new_context(viewport={"width": width, "height": 1000})
            context.add_init_script(f"Date.now = () => {NOW_MS};")
            contexts.append(context)
        tab = Tab(context, base, slug)
        tabs.append(tab)
        return tab

    yield make
    for tab in tabs:
        assert tab.errors == []
    for context in contexts:
        context.close()
    layout.path().unlink(missing_ok=True)


def saved(expected_rows, page=None, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = layout.read()
        if set(current) == set(expected_rows) and (page is None or current == (page.stored() or {})):
            return current
        time.sleep(0.05)
    if page is not None:
        pytest.fail(f"server layout {layout.read()} never matched the page's {page.stored()}")
    return layout.read()


def test_each_height_handle_drags_and_the_size_survives_a_reload(visit):
    page = visit()
    before = page.heights()
    for row in HEIGHT_ROWS:
        page.drag("height", row, dy=-30 if row == "health-box" else 40)
    after = page.heights()
    for row in HEIGHT_ROWS:
        assert abs(after[row] - before[row] - (-30 if row == "health-box" else 40)) <= 2, (row, before, after)
    server_side = saved(HEIGHT_ROWS, page)
    assert {row: server_side[row]["height"] for row in HEIGHT_ROWS} == pytest.approx(after, abs=1)
    page.reload()
    assert page.heights() == pytest.approx(after, abs=1)


def test_a_paired_row_height_sets_both_panels_together(visit):
    page = visit()
    for row, (left, right) in PAIRS.items():
        page.drag("height", row, dy=-60)
        height = page.box(row)["height"]
        assert page.box(left)["height"] == pytest.approx(height, abs=1)
        assert page.box(right)["height"] == pytest.approx(height, abs=1)


def test_each_split_handle_moves_only_its_own_row_and_the_row_stays_full_width(visit):
    page = visit()
    full = page.box("swarm-box")["width"]
    accounts_before = page.share("swarm-row-accounts")
    page.split_to("swarm-row-work", 70)
    assert page.share("swarm-row-work") == pytest.approx(70, abs=1.5)
    assert page.share("swarm-row-accounts") == pytest.approx(accounts_before, abs=0.5)
    page.split_to("swarm-row-accounts", 30)
    assert page.share("swarm-row-accounts") == pytest.approx(30, abs=1.5)
    assert page.share("swarm-row-work") == pytest.approx(70, abs=1.5)
    for row_id, (left, right) in PAIRS.items():
        assert page.box(row_id)["width"] == pytest.approx(full, abs=1)
        a, b = page.box(left), page.box(right)
        assert b["x"] + b["width"] == pytest.approx(page.box(row_id)["x"] + full, abs=1)
        assert a["x"] + a["width"] <= b["x"] + 1
    assert {row: entry["split"] for row, entry in saved(PAIRS, page).items()} == {
        "swarm-row-work": pytest.approx(70, abs=1.5),
        "swarm-row-accounts": pytest.approx(30, abs=1.5),
    }
    page.reload()
    assert page.share("swarm-row-work") == pytest.approx(70, abs=1.5)
    assert page.share("swarm-row-accounts") == pytest.approx(30, abs=1.5)


def test_a_second_ledger_and_a_fresh_browser_open_with_the_same_layout(visit):
    page = visit()
    page.drag("height", "health-box", dy=-40)
    page.drag("split", "swarm-row-work", dx=-80)
    saved(("health-box", "swarm-row-work"), page)
    sizes, share = page.heights(), page.share("swarm-row-work")
    page.open(SLUGS[1])
    assert page.heights() == pytest.approx(sizes, abs=1)
    assert page.share("swarm-row-work") == pytest.approx(share, abs=0.5)
    fresh = visit(SLUGS[1])
    assert fresh.stored() == layout.read()
    assert fresh.heights() == pytest.approx(sizes, abs=1)
    assert fresh.share("swarm-row-work") == pytest.approx(share, abs=0.5)


def test_a_pause_mid_drag_still_leaves_the_final_split_on_the_server(visit):
    page = visit()
    handle = page.grip("split", "swarm-row-work")
    handle.scroll_into_view_if_needed()
    box = handle.bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.tab.mouse.move(x, y)
    page.tab.mouse.down()
    page.tab.mouse.move(x - 40, y)
    midpoint = saved(("swarm-row-work",), page)
    page.tab.mouse.move(x - 80, y)
    page.tab.mouse.up()
    final = saved(("swarm-row-work",), page)
    assert final != midpoint
    share = page.share("swarm-row-work")
    page.open(SLUGS[1])
    assert page.share("swarm-row-work") == pytest.approx(share, abs=0.5)


def test_local_storage_paints_the_layout_while_the_server_is_unreachable(visit):
    page = visit()
    page.drag("height", "handoff-box", dy=50)
    saved(("handoff-box",), page)
    height = page.box("handoff-box")["height"]
    page.tab.route("**/api/layout", lambda route: route.abort())
    page.reload()
    assert page.box("handoff-box")["height"] == pytest.approx(height, abs=1)


def test_panel_content_scrolls_inside_a_panel_smaller_than_its_content(visit):
    page = visit()
    page.drag("height", "health-box", dy=-120)
    page.drag("height", "swarm-row-work", dy=-120)
    for panel in ("health-box", "agents-box", "overlays-box"):
        fits = page.tab.eval_on_selector(f"#{panel}", "el => el.scrollHeight <= el.clientHeight + 1")
        assert fits, panel
    inner = page.tab.eval_on_selector("#health-box .sw-scroll", "el => [el.scrollHeight, el.clientHeight]")
    assert inner[0] > inner[1], inner
    page.tab.eval_on_selector("#health-box .sw-scroll", "el => el.scrollTop = el.scrollHeight")
    assert page.tab.eval_on_selector("#health-box .sw-scroll", "el => el.scrollTop") > 0


def test_arrow_keys_move_a_focused_handle(visit):
    page = visit()
    before, share = page.box("handoff-box")["height"], page.share("swarm-row-accounts")
    page.grip("height", "handoff-box").focus()
    page.tab.keyboard.press("ArrowDown")
    page.tab.keyboard.press("ArrowDown")
    assert page.box("handoff-box")["height"] == pytest.approx(before + 32, abs=1)
    page.tab.keyboard.press("ArrowUp")
    assert page.box("handoff-box")["height"] == pytest.approx(before + 16, abs=1)
    page.grip("split", "swarm-row-accounts").focus()
    page.tab.keyboard.press("ArrowLeft")
    page.tab.keyboard.press("ArrowLeft")
    assert page.share("swarm-row-accounts") == pytest.approx(share - 4, abs=1)
    page.tab.keyboard.press("ArrowRight")
    assert page.share("swarm-row-accounts") == pytest.approx(share - 2, abs=1)
    assert set(saved(("handoff-box", "swarm-row-accounts"), page)) == {"handoff-box", "swarm-row-accounts"}
    labels = page.tab.eval_on_selector_all(
        "[data-grip]",
        "gs => gs.map(g => [g.getAttribute('role'), g.getAttribute('aria-orientation'), g.tabIndex, !!g.getAttribute('aria-label')])",
    )
    assert len(labels) == 7
    assert all(role == "separator" and index == 0 and named for role, _, index, named in labels)
    assert sorted(o for _, o, _, _ in labels) == ["horizontal"] * 5 + ["vertical"] * 2


def test_handles_are_flat_and_show_only_on_hover_or_focus(visit):
    page = visit()
    page.tab.mouse.move(1, 1)
    handle = page.grip("height", "health-box")
    line = "el => { const s = getComputedStyle(el, '::after'); return [s.opacity, s.boxShadow, s.backgroundImage]; }"
    rest = handle.evaluate(line)
    assert rest[0] == "0"
    assert handle.evaluate("el => getComputedStyle(el).backgroundColor") == "rgba(0, 0, 0, 0)"
    handle.hover()
    page.tab.wait_for_function(
        "() => getComputedStyle(document.querySelector('[data-grip=height][data-row=health-box]'), '::after').opacity === '1'"
    )
    shown = handle.evaluate(line)
    assert shown[2] == "none"
    assert handle.evaluate("el => getComputedStyle(el).cursor") == "row-resize"
    assert page.grip("split", "swarm-row-work").evaluate("el => getComputedStyle(el).cursor") == "col-resize"


def test_reset_layout_restores_the_defaults_everywhere(visit):
    page = visit()
    defaults, shares = page.heights(), {row: page.share(row) for row in PAIRS}
    assert page.tab.locator("#layout-reset").is_disabled()
    for row in HEIGHT_ROWS:
        page.drag("height", row, dy=30)
    page.drag("split", "swarm-row-accounts", dx=120)
    saved((*HEIGHT_ROWS,), page)
    assert page.tab.locator("#layout-reset").is_enabled()
    page.tab.locator("#layout-reset").click()
    assert page.heights() == pytest.approx(defaults, abs=1)
    assert {row: page.share(row) for row in PAIRS} == pytest.approx(shares, abs=0.5)
    assert saved((), page) == {}
    assert page.stored() is None
    assert page.tab.locator("#layout-reset").is_disabled()
    page.reload()
    assert page.heights() == pytest.approx(defaults, abs=1)


def test_phone_width_stacks_the_panels_hides_the_handles_and_ignores_saved_sizes(visit):
    layout.write({"capacity-box": {"height": 400}, "swarm-row-work": {"height": 90, "split": 20}})
    page = visit(width=390)
    assert page.tab.locator("[data-grip]").count() == 7
    assert page.tab.eval_on_selector_all("[data-grip]", "gs => gs.filter(g => g.getClientRects().length).length") == 0
    for left, right in PAIRS.values():
        a, b = page.box(left), page.box(right)
        assert b["y"] >= a["y"] + a["height"] - 1
        assert a["width"] == pytest.approx(b["width"], abs=1)
    assert page.box("capacity-box")["height"] < 400
    assert page.tab.eval_on_selector("#swarm-row-work", "el => el.scrollHeight <= el.clientHeight + 1")
    assert page.stored() == layout.read()
