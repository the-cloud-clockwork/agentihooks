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
HEIGHT_ROWS = ("capacity-box", "quota-box", "hives-box", "overlays-box", "agents-box", "handoff-box")
HANDOFFS = [
    {
        "seat": f"eng-{n}@rig",
        "at": NOW_MS - 60_000,
        "reason": "recycle",
        "continuity": "confirmed",
        "binding": "live",
        "successor": f"eng-{n + 12}",
        "awaiting": "",
    }
    for n in range(12)
]


@pytest.fixture(scope="module")
def base(ledger_dir):
    for slug in SLUGS:
        content = {"title": slug, "overview": "o", "sources": [], "phases": [], "questions": [], "followups": []}
        html_path, _ = legacy_page.paths(slug, core)
        html_path.write_text(legacy_page.render(new_ledger.build_doc(content), slug, 8765), encoding="utf-8")
        core.sync(slug)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    port = httpd.server_address[1]
    hosts = {f"127.0.0.1:{port}"}
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(server, "ALLOWED_HOSTS", hosts)
        patch.setattr(server, "ALLOWED_ORIGINS", {f"http://{host}" for host in hosts})
        patch.setattr(server, "swarm_status", lambda slug, state=None: status(handoffs=HANDOFFS))
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

    def heights(self):
        return {row: round(self.box(row)["height"]) for row in HEIGHT_ROWS}

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
        page.drag("height", row, dy=-30 if row == "handoff-box" else 40)
    after = page.heights()
    for row in HEIGHT_ROWS:
        assert abs(after[row] - before[row] - (-30 if row == "handoff-box" else 40)) <= 2, (row, before, after)
    server_side = saved(HEIGHT_ROWS, page)
    assert {row: server_side[row]["height"] for row in HEIGHT_ROWS} == pytest.approx(after, abs=1)
    page.reload()
    assert page.heights() == pytest.approx(after, abs=1)


def test_each_row_stays_full_width_after_a_height_drag(visit):
    page = visit()
    full = page.box("swarm-box")["width"]
    page.drag("height", "agents-box", dy=-60)
    for row in HEIGHT_ROWS:
        assert page.box(row)["width"] == pytest.approx(full, abs=1)


def test_a_second_ledger_and_a_fresh_browser_open_with_the_same_layout(visit):
    page = visit()
    page.drag("height", "handoff-box", dy=-40)
    page.drag("height", "agents-box", dy=-80)
    saved(("handoff-box", "agents-box"), page)
    sizes = page.heights()
    page.open(SLUGS[1])
    assert page.heights() == pytest.approx(sizes, abs=1)
    fresh = visit(SLUGS[1])
    assert fresh.stored() == layout.read()
    assert fresh.heights() == pytest.approx(sizes, abs=1)


def test_a_pause_mid_drag_still_leaves_the_final_height_on_the_server(visit):
    page = visit()
    handle = page.grip("height", "agents-box")
    handle.scroll_into_view_if_needed()
    box = handle.bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.tab.mouse.move(x, y)
    page.tab.mouse.down()
    page.tab.mouse.move(x, y - 40)
    midpoint = saved(("agents-box",), page)
    page.tab.mouse.move(x, y - 80)
    page.tab.mouse.up()
    final = saved(("agents-box",), page)
    assert final != midpoint
    height = page.box("agents-box")["height"]
    page.open(SLUGS[1])
    assert page.box("agents-box")["height"] == pytest.approx(height, abs=1)


def test_local_storage_paints_the_layout_while_the_server_is_unreachable(visit):
    page = visit()
    page.drag("height", "handoff-box", dy=50)
    saved(("handoff-box",), page)
    height = page.box("handoff-box")["height"]
    page.tab.route("**/api/v1/layout", lambda route: route.abort())
    page.reload()
    assert page.box("handoff-box")["height"] == pytest.approx(height, abs=1)


def test_a_stored_layout_with_a_retired_row_saves_without_it(browser, visit):
    retired = '{"health-box": {"height": 200}, "agents-box": {"height": 300}}'
    context = browser.new_context(viewport={"width": 1440, "height": 1000})
    context.add_init_script(f"Date.now = () => {NOW_MS};")
    context.add_init_script(f"localStorage.setItem('plan-ledger:swarm-layout', '{retired}');")
    context.route(
        "**/api/v1/layout", lambda route: route.abort() if route.request.method == "GET" else route.fallback()
    )
    try:
        page = visit(context=context)
        assert page.box("agents-box")["height"] == pytest.approx(300, abs=1)
        page.drag("height", "handoff-box", dy=-40)
        server_side = saved(("agents-box", "handoff-box"))
        assert server_side["agents-box"] == {"height": 300}
        assert "health-box" not in page.tab.evaluate("() => localStorage.getItem('plan-ledger:swarm-layout')")
    finally:
        context.close()


def test_panel_content_scrolls_inside_a_panel_smaller_than_its_content(visit):
    page = visit()
    page.drag("height", "handoff-box", dy=-120)
    page.drag("height", "agents-box", dy=-120)
    page.drag("height", "overlays-box", dy=-120)
    for panel in ("handoff-box", "agents-box", "overlays-box"):
        fits = page.tab.eval_on_selector(f"#{panel}", "el => el.scrollHeight <= el.clientHeight + 1")
        assert fits, panel
    inner = page.tab.eval_on_selector("#handoff-box .sw-scroll", "el => [el.scrollHeight, el.clientHeight]")
    assert inner[0] > inner[1], inner
    page.tab.eval_on_selector("#handoff-box .sw-scroll", "el => el.scrollTop = el.scrollHeight")
    assert page.tab.eval_on_selector("#handoff-box .sw-scroll", "el => el.scrollTop") > 0


def test_arrow_keys_move_a_focused_handle(visit):
    page = visit()
    before = page.box("handoff-box")["height"]
    page.grip("height", "handoff-box").focus()
    page.tab.keyboard.press("ArrowDown")
    page.tab.keyboard.press("ArrowDown")
    assert page.box("handoff-box")["height"] == pytest.approx(before + 32, abs=1)
    page.tab.keyboard.press("ArrowUp")
    assert page.box("handoff-box")["height"] == pytest.approx(before + 16, abs=1)
    saved(("handoff-box",), page)
    labels = page.tab.eval_on_selector_all(
        "[data-grip]",
        "gs => gs.map(g => [g.getAttribute('role'), g.getAttribute('aria-orientation'), g.tabIndex, !!g.getAttribute('aria-label')])",
    )
    assert len(labels) == len(HEIGHT_ROWS)
    assert all(role == "separator" and index == 0 and named for role, _, index, named in labels)
    assert {o for _, o, _, _ in labels} == {"horizontal"}


def test_handles_are_flat_and_show_only_on_hover_or_focus(visit):
    page = visit()
    page.tab.mouse.move(1, 1)
    handle = page.grip("height", "handoff-box")
    line = "el => { const s = getComputedStyle(el, '::after'); return [s.opacity, s.boxShadow, s.backgroundImage]; }"
    rest = handle.evaluate(line)
    assert rest[0] == "0"
    assert handle.evaluate("el => getComputedStyle(el).backgroundColor") == "rgba(0, 0, 0, 0)"
    handle.hover()
    page.tab.wait_for_function(
        "() => getComputedStyle(document.querySelector('[data-grip=height][data-row=handoff-box]'), '::after').opacity === '1'"
    )
    shown = handle.evaluate(line)
    assert shown[2] == "none"
    assert handle.evaluate("el => getComputedStyle(el).cursor") == "row-resize"


def test_reset_layout_restores_the_defaults_everywhere(visit):
    page = visit()
    defaults = page.heights()
    assert page.tab.locator("#layout-reset").is_disabled()
    for row in HEIGHT_ROWS:
        page.drag("height", row, dy=30)
    saved((*HEIGHT_ROWS,), page)
    assert page.tab.locator("#layout-reset").is_enabled()
    page.tab.locator("#layout-reset").click()
    assert page.heights() == pytest.approx(defaults, abs=1)
    assert saved((), page) == {}
    assert page.stored() is None
    assert page.tab.locator("#layout-reset").is_disabled()
    page.reload()
    assert page.heights() == pytest.approx(defaults, abs=1)


def test_phone_width_stacks_the_panels_hides_the_handles_and_ignores_saved_sizes(visit):
    layout.write({"capacity-box": {"height": 640}, "agents-box": {"height": 90}})
    page = visit(width=390)
    assert page.tab.locator("[data-grip]").count() == len(HEIGHT_ROWS)
    assert page.tab.eval_on_selector_all("[data-grip]", "gs => gs.filter(g => g.getClientRects().length).length") == 0
    full = page.box("swarm-box")["width"]
    for row in HEIGHT_ROWS:
        assert page.box(row)["width"] == pytest.approx(full, abs=1)
    assert page.box("capacity-box")["height"] < 640
    assert page.tab.eval_on_selector("#agents-box", "el => el.scrollHeight <= el.clientHeight + 1")
    assert page.stored() == layout.read()


def saved_columns(key, timeout=5.0, settled=lambda widths: True):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = layout.read().get("columns", {}).get(key)
        if found and settled(found):
            return found
        time.sleep(0.05)
    raise AssertionError(f"no saved widths for {key}: {layout.read()}")


def column_width(page, table, index=0):
    return round(page.tab.locator(f"#{table} thead th").nth(index).bounding_box()["width"])


def test_a_column_grip_drags_its_width_and_the_width_survives_a_reload(visit):
    page = visit(width=1920)
    grip = page.tab.locator("#handoff-table thead th").first.locator(".col-grip")
    grip.scroll_into_view_if_needed()
    before = column_width(page, "handoff-table")
    box = grip.bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.tab.mouse.move(x, y)
    page.tab.mouse.down()
    page.tab.mouse.move(x + 40, y)
    page.tab.mouse.move(x + 80, y)
    page.tab.mouse.up()
    widths = saved_columns("handoff-table-6", settled=lambda found: abs(found[0] - (before + 80)) <= 2)
    assert abs(widths[0] - (before + 80)) <= 2
    page.reload()
    assert abs(column_width(page, "handoff-table") - widths[0]) <= 1
    assert "sized-cols" in page.tab.get_attribute("#handoff-table", "class")


def test_arrow_keys_move_a_focused_column_grip_and_reset_clears_the_widths(visit):
    page = visit(width=1920)
    grip = page.tab.locator("#handoff-table thead th").first.locator(".col-grip")
    grip.scroll_into_view_if_needed()
    before = column_width(page, "handoff-table")
    grip.focus()
    page.tab.keyboard.press("ArrowRight")
    page.tab.keyboard.press("Shift+ArrowRight")
    page.tab.keyboard.press("Enter")
    page.tab.wait_for_function(
        f"() => Math.round(document.querySelector('#handoff-table thead th').getBoundingClientRect().width) >= {before + 79}"
    )
    assert abs(saved_columns("handoff-table-6")[0] - (before + 80)) <= 2
    page.tab.locator("#layout-reset").click()
    page.tab.wait_for_function("() => !document.querySelector('#handoff-table').classList.contains('sized-cols')")
    deadline = time.monotonic() + 5
    while "columns" in layout.read() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert "columns" not in layout.read()


def test_home_columns_drag_and_step_and_keep_their_widths(browser, base):
    layout.path().unlink(missing_ok=True)
    context = browser.new_context(viewport={"width": 1920, "height": 1000})
    tab = context.new_page()
    try:
        tab.goto(f"{base}/")
        grip = tab.locator(".row.head > *:nth-child(3) .col-grip")
        grip.wait_for()

        def tracks():
            found = tab.evaluate("() => getComputedStyle(document.querySelector('.row.head')).gridTemplateColumns")
            return [float(t) for t in found.replace("px", "").split()]

        before = tracks()
        box = grip.bounding_box()
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        tab.mouse.move(x, y)
        tab.mouse.down()
        tab.mouse.move(x - 100, y)
        tab.mouse.up()
        grip.focus()
        tab.keyboard.press("ArrowLeft")
        widths = saved_columns("home", settled=lambda found: abs(found[2] - (before[2] - 116)) <= 2)
        assert abs(widths[2] - (before[2] - 116)) <= 2
        assert widths[:2] == [round(w) for w in before[:2]]
        tab.locator(".sort[data-sort=kind]").click()
        assert tab.locator(".sort[data-sort=kind] i").text_content() in ("▲", "▼")
        tab.reload()
        tab.locator(".row.head").wait_for()
        tab.wait_for_function("() => document.querySelector('.panel').classList.contains('sized-cols')")
        assert abs(tracks()[2] - widths[2]) <= 1
    finally:
        context.close()
        layout.path().unlink(missing_ok=True)
