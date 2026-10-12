from playwright.sync_api import expect

from tests.swarm_ledger import test_swarm_layout as layout

browser = layout.browser
open_page = layout.open_page

CATALOG = {
    "claude": [{"model": "opus", "label": "Claude Opus"}, {"model": "future-claude", "label": "Future Claude"}],
    "codex": [{"model": "gpt-6.1-sol", "label": "Sol 6.1"}, {"model": "future-sol", "label": "Future Sol"}],
}


def model_page(open_page, catalog=CATALOG):
    payload = layout.status()
    payload["config"].update(claude_model="opus", codex_model="gpt-6.1-sol")
    page = open_page(payload, width=1920)
    page.tab.set_viewport_size({"width": 1920, "height": 1080})
    page.context.route("**/api/swarm/*/models", lambda route: route.fulfill(json=catalog))
    page.tab.reload()
    expect(page.tab.locator("#cap-claude_model")).to_have_value("opus")
    return page


def test_select_options_come_only_from_catalog_route(open_page):
    page = model_page(open_page)
    for harness in ("claude", "codex"):
        options = page.tab.locator(f"#cap-{harness}_model option")
        expect(options).to_have_count(2)
        assert options.evaluate_all("os => os.map(o => [o.value, o.textContent])") == [
            [entry["model"], entry["label"]] for entry in CATALOG[harness]
        ]


def test_empty_catalog_shows_only_current_value(open_page):
    page = model_page(open_page, {"claude": [], "codex": []})
    for harness, current in (("claude", "opus"), ("codex", "gpt-6.1-sol")):
        expect(page.tab.locator(f"#cap-{harness}_model option")).to_have_count(1)
        expect(page.tab.locator(f"#cap-{harness}_model")).to_have_value(current)


def test_populated_catalog_does_not_add_an_absent_current_model(open_page):
    page = model_page(open_page)
    page.context.route(
        "**/api/swarm/*/models", lambda route: route.fulfill(json={"claude": CATALOG["claude"][1:], "codex": []})
    )
    page.tab.reload()
    expect(page.tab.locator("#cap-claude_model option")).to_have_count(1)
    expect(page.tab.locator("#cap-claude_model option")).to_have_attribute("value", "future-claude")


def test_apply_sends_changed_models_and_xhigh_in_one_body(open_page):
    page = model_page(open_page)
    page.tab.locator("#cap-claude_model").select_option("future-claude")
    page.tab.locator("#cap-codex_model").select_option("future-sol")
    page.tab.locator('[data-swarm="effort_max_up"]').click()
    expect(page.tab.locator("#cap-effort_max")).to_have_value("xhigh")
    page.tab.locator('[data-swarm="apply"]').click()
    for _ in range(100):
        if page.puts:
            break
        page.tab.wait_for_timeout(100)
    assert page.puts == [
        {"action": "set", "effort_max": "xhigh", "claude_model": "future-claude", "codex_model": "future-sol"}
    ]


def test_unknown_model_is_refused_before_apply(open_page):
    page = model_page(open_page)
    page.tab.locator("#cap-claude_model").evaluate(
        "s => {s.add(new Option('unknown', 'unknown')); s.value='unknown'; s.dispatchEvent(new Event('input', {bubbles:true}));}"
    )
    page.tab.locator('[data-swarm="apply"]').click()
    expect(page.tab.locator("#swarm-note")).to_contain_text("catalog")
    assert page.puts == []
