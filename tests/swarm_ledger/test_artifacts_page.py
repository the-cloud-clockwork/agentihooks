import json
import time
from pathlib import Path

import pytest

from tests.swarm_ledger.test_artifacts import JSON_DOC, MARKDOWN, SVG
from tests.swarm_ledger.test_bin import DAY_MS
from tests.swarm_ledger.test_caps_columns import browser as chromium_browser

TEMPLATE = Path(__file__).resolve().parents[2] / "scripts/swarm_ledger/template.html"
browser = chromium_browser
FILES = {
    "a" * 64 + ".md": (MARKDOWN, "text/markdown"),
    "b" * 64 + ".json": (JSON_DOC, "application/json"),
    "c" * 64 + ".svg": (SVG, "image/svg+xml"),
}
ROWS = [
    {
        "id": "art-md",
        "title": "Handoff template proposal",
        "by": "engineer@1",
        "task": "av1",
        "at": 1_000,
        "file": {"id": "a" * 64 + ".md", "type": "text/markdown", "size": len(MARKDOWN)},
    },
    {
        "id": "art-json",
        "title": "Proposal shape",
        "by": "engineer@2",
        "task": "hd1",
        "at": 2_000,
        "file": {"id": "b" * 64 + ".json", "type": "application/json", "size": len(JSON_DOC)},
    },
    {
        "id": "art-svg",
        "title": "Handoff flow diagram",
        "by": "engineer@3",
        "task": "av1",
        "at": 3_000,
        "file": {"id": "c" * 64 + ".svg", "type": "image/svg+xml", "size": len(SVG)},
    },
]


def serve(route):
    body, ctype = FILES[route.request.url.rsplit("/", 1)[1]]
    route.fulfill(body=body, content_type=ctype)


@pytest.fixture(params=[(1440, 900), (1249, 900), (390, 844)], ids=["desktop", "narrow-desktop", "phone"])
def page(browser, request):
    width, height = request.param
    tab = browser.new_page(viewport={"width": width, "height": height})
    tab.set_default_timeout(1500)
    tab.route("**/artifacts/**", serve)
    doc = {"title": "Artifacts proof", "tasks": [{"id": "av1", "title": "Artifacts"}], "artifacts": ROWS}
    html = TEMPLATE.read_text().replace("__LEDGER_DATA__", json.dumps(doc))
    html = html.replace("__LEDGER_PALETTE__", (TEMPLATE.parent / "palette.css").read_text())
    html = html.replace("__LEDGER_PORT__", "8765").replace("__LEDGER_SLUG__", "arts")
    tab.set_content(html)
    yield tab
    tab.close()


def open_artifact(tab, title):
    tab.get_by_role("button", name="Artifacts").click()
    tab.locator("#art-list").get_by_role("button", name=title, exact=True).click()
    viewer = tab.get_by_role("dialog", name="Artifact viewer")
    assert viewer.is_visible()
    assert len(tab.context.pages) == 1
    return viewer


def test_icon_lists_every_artifact_newest_first_with_author_task_and_time(page):
    page.get_by_role("button", name="Artifacts").click()
    rows = page.locator("#art-list .art-row")
    assert rows.count() == 3
    assert [rows.nth(i).locator(".art-title").inner_text() for i in range(3)] == [
        "Handoff flow diagram",
        "Proposal shape",
        "Handoff template proposal",
    ]
    meta = rows.nth(1).locator(".notif-meta").inner_text()
    assert "engineer@2" in meta and "#hd1" in meta
    badge = page.locator("#art-badge")
    assert badge.inner_text() == "3"


def test_markdown_renders_headings_tables_and_code(page):
    viewer = open_artifact(page, "Handoff template proposal")
    body = viewer.locator(".art-body")
    assert body.locator("h1").inner_text() == "Handoff template"
    assert body.locator("table th").all_inner_texts() == ["Field", "Use"]
    assert body.locator("table td").all_inner_texts() == ["Done", "what landed"]
    assert body.locator("pre code").inner_text().strip() == "agentihooks swarm done"
    assert body.locator("ul li").evaluate("el => getComputedStyle(el).listStyleType") == "disc"
    box = viewer.bounding_box()
    width = page.viewport_size["width"]
    assert box["width"] >= width - 40
    page.keyboard.press("Escape")
    assert not viewer.is_visible()


def test_inline_code_and_links_nested_in_bold_and_italic_are_formatted(page):
    nested = b"**Verdict `early-real`** and *see `ledger say` [docs](https://example.com)*\n"
    page.route(
        "**/artifacts/**/" + "a" * 64 + ".md", lambda route: route.fulfill(body=nested, content_type="text/markdown")
    )
    body = open_artifact(page, "Handoff template proposal").locator(".art-body")
    assert body.locator("strong").inner_text() == "Verdict early-real"
    assert body.locator("strong code").inner_text() == "early-real"
    assert body.locator("em code").inner_text() == "ledger say"
    assert body.locator("em a").get_attribute("href") == "https://example.com"
    assert "`" not in body.inner_text()


def test_json_is_pretty_printed_and_foldable(page):
    viewer = open_artifact(page, "Proposal shape")
    body = viewer.locator(".art-body")
    folds = body.locator("details")
    folds.nth(2).wait_for()
    assert folds.count() == 3
    assert "Stopped at" in body.inner_text()
    assert body.locator("summary").nth(1).inner_text() == '"proposal": {'
    folds.nth(1).locator("summary").first.click()
    assert not folds.nth(1).evaluate("el => el.open")
    assert not body.get_by_text('"Stopped at"').is_visible()
    viewer.get_by_role("button", name="Close artifact").click()
    assert not viewer.is_visible()


def test_svg_renders_as_an_image_and_its_script_never_runs(page):
    viewer = open_artifact(page, "Handoff flow diagram")
    viewer.locator(".art-body .hint").wait_for(state="detached")
    page.wait_for_timeout(200)
    assert page.evaluate("window.pwned") is None
    image = viewer.locator(".art-body img")
    page.wait_for_function("img => img.complete && img.naturalWidth > 0", arg=image.element_handle())
    assert image.bounding_box()["width"] <= viewer.locator(".art-body").bounding_box()["width"]
    assert image.get_attribute("src").endswith(".svg")


@pytest.fixture
def trash_page(browser):
    tab = browser.new_page(viewport={"width": 1440, "height": 900})
    tab.set_default_timeout(1500)
    tab.route("**/artifacts/**", serve)
    sent = []

    def api(route):
        if route.request.method == "PUT":
            sent.extend(json.loads(route.request.post_data)["ops"])
        route.fulfill(status=503, body="offline")

    tab.route("**/api/**", api)
    old = {**ROWS[0], "id": "art-old", "title": "Old logo draft", "deleted_at": int(time.time() * 1000) - 2 * DAY_MS}
    doc = {"title": "Trash proof", "tasks": [], "artifacts": ROWS[1:], "artifact_trash": [old]}
    html = TEMPLATE.read_text().replace("__LEDGER_DATA__", json.dumps(doc))
    html = html.replace("__LEDGER_PALETTE__", (TEMPLATE.parent / "palette.css").read_text())
    html = html.replace("__LEDGER_PORT__", "8765").replace("__LEDGER_SLUG__", "arts")
    tab.set_content(html)
    tab.get_by_role("button", name="Artifacts").click()
    yield tab, sent
    tab.close()


def test_delete_moves_an_artifact_to_the_trash_and_drops_the_badge(trash_page):
    tab, sent = trash_page
    assert tab.locator("#art-badge").inner_text() == "2"
    tab.get_by_role("button", name="Delete Proposal shape").click()
    assert tab.locator("#art-list .art-row").count() == 1
    assert tab.locator("#art-badge").inner_text() == "1"
    assert "Proposal shape" in tab.locator("#art-trash").inner_text()
    tab.wait_for_function("() => document.getElementById('status').textContent !== ''")
    tab.wait_for_timeout(200)
    assert {"op": "artifact_delete", "target": "art-json"}.items() <= sent[0].items()


def test_the_trash_lists_days_left_and_restore_brings_an_artifact_back(trash_page):
    tab, _ = trash_page
    row = tab.locator("#art-trash .art-row", has_text="Old logo draft")
    assert "28 days left" in row.inner_text()
    row.get_by_role("button", name="Restore Old logo draft").click()
    assert tab.locator("#art-badge").inner_text() == "3"
    assert tab.locator("#art-list").get_by_role("button", name="Old logo draft", exact=True).is_visible()
    assert tab.locator("#art-trash .art-row").count() == 0
