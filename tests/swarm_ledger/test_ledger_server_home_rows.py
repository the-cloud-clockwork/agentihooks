import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"))

from scripts.swarm_ledger import ledger_server as server  # noqa: E402

URL = "http://ledger.test/"
LONG = " ".join(["The overview runs on well past the width of its column."] * 12)
LEDGERS = {
    "a": ("swarm", 3, 1, "running", 0, 300),
    "b": ("small", 9, 0, None, 0, 500),
    "c": ("swarm", 1, 7, "stopped", 5, 100),
    "d": ("small", 0, 2, "paused", 0, 400),
    "e": ("swarm", 5, 3, "drained", 0, 200),
    "f": ("swarm", 6, 4, "stopped", 0, 50),
    "g": ("swarm", 2, 5, "stopping", 0, 600),
}
BLOCKED_STORAGE = (
    """Object.defineProperty(window, "localStorage", { get() { throw new Error("storage blocked"); } });"""
)


def summaries():
    return [
        {
            "slug": slug,
            "title": f"Ledger {slug} " + ("with a title long enough to need a second line " * 3 if slug == "a" else ""),
            "overview": LONG if slug == "a" else f"Overview {slug}",
            "size": size,
            "open": open_,
            "done": done,
            "closed_at": closed,
            "updated_at": at * 60000,
        }
        for slug, (size, open_, done, _, closed, at) in LEDGERS.items()
    ]


def home_html(view="home"):
    with (
        patch.object(server, "ledger_summaries", return_value=summaries()),
        patch.object(server, "bin_summaries", return_value=[{**summaries()[0], "deleted_at": 0, "days_left": 3}]),
        patch.object(server, "swarm_state", side_effect=lambda slug: LEDGERS[slug][3]),
        patch.object(server.ledger_bin, "entries", return_value={}),
    ):
        return server.index_page(view, now=700 * 60000)


def row_attrs(page):
    return {m.group(1): m.group(0) for m in re.finditer(r'<li class="row" data-slug="(\w)"[^>]*>', page)}


def test_home_rows_carry_the_values_their_columns_sort_by():
    rows = row_attrs(home_html())
    assert set(rows) == set(LEDGERS)
    assert 'data-kind="small"' in rows["b"]
    assert 'data-open="9"' in rows["b"]
    assert 'data-done="7"' in rows["c"]
    assert f'data-at="{600 * 60000}"' in rows["g"]


@pytest.mark.parametrize(("slug", "rank"), [("a", 0), ("d", 1), ("g", 1), ("e", 2), ("f", 3), ("b", 4), ("c", 5)])
def test_swarm_rank_runs_running_paused_drained_stopped_none_closed(slug, rank):
    assert f'data-swarm="{rank}"' in row_attrs(home_html())[slug]


def test_unknown_swarm_state_ranks_with_no_swarm():
    assert server.swarm_rank("rebooting") == server.swarm_rank(None) == 4


def test_exactly_kind_open_done_swarm_and_activity_headers_sort():
    page = home_html()
    assert re.findall(r'data-sort="(\w+)"', page) == ["kind", "open", "done", "swarm", "at"]
    assert page.count('class="fold"') == len(LEDGERS)
    assert 'id="fold-all"' in page


def test_bin_has_no_fold_or_sort_controls():
    page = home_html("bin")
    assert "data-sort=" not in page
    assert 'class="fold"' not in page
    assert 'id="fold-all"' not in page


@pytest.fixture(scope="module")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as pw:
        try:
            chromium = pw.chromium.launch()
        except Exception as exc:
            pytest.skip(f"no chromium: {exc}")
        yield chromium
        chromium.close()


@pytest.fixture
def context(browser):
    context = browser.new_context(viewport={"width": 1920, "height": 1080})
    context.set_default_timeout(5000)
    html = home_html()
    context.route(
        "**/*",
        lambda route: route.fulfill(body=html, content_type="text/html") if route.request.url == URL else route.abort(),
    )
    yield context
    context.close()


@pytest.fixture
def tab(context):
    page = context.new_page()
    page.goto(URL)
    return page


def order(tab):
    return tab.evaluate("() => [...document.querySelectorAll('li.row')].map((r) => r.dataset.slug).join('')")


def arrows(tab):
    return tab.evaluate(
        "() => Object.fromEntries([...document.querySelectorAll('.sort')].map((b) => [b.dataset.sort, b.querySelector('i').textContent]))"
    )


def heights(tab):
    return tab.evaluate("() => [...document.querySelectorAll('li.row')].map((r) => r.getBoundingClientRect().height)")


def opened(tab):
    return tab.evaluate("() => [...document.querySelectorAll('li.row.open')].map((r) => r.dataset.slug).join('')")


def test_rows_start_folded_on_one_line_of_equal_height(tab):
    assert opened(tab) == ""
    assert len(set(heights(tab))) == 1
    assert tab.locator("#fold-all").text_content() == "Expand all"
    assert (
        tab.evaluate("() => getComputedStyle(document.querySelector('li.row[data-slug=a] .ov')).whiteSpace") == "nowrap"
    )


def test_the_row_arrow_opens_the_full_title_and_overview_and_closes_it_again(tab):
    row = tab.locator("li.row[data-slug=a]")
    folded = row.bounding_box()["height"]
    row.locator(".fold").click()
    assert row.locator(".fold").get_attribute("aria-expanded") == "true"
    assert row.bounding_box()["height"] > folded * 3
    full = tab.evaluate(
        "() => { const o = document.querySelector('li.row[data-slug=a] .ov'); return o.scrollWidth <= o.clientWidth; }"
    )
    assert full
    row.locator(".fold").click()
    assert row.bounding_box()["height"] == folded


def test_fold_choices_and_expand_all_are_remembered_across_reloads(tab):
    tab.locator("li.row[data-slug=b] .fold").click()
    tab.reload()
    assert opened(tab) == "b"
    tab.locator("#fold-all").click()
    assert len(opened(tab)) == len(LEDGERS)
    assert tab.locator("#fold-all").text_content() == "Collapse all"
    tab.reload()
    assert len(opened(tab)) == len(LEDGERS)
    assert tab.locator("#fold-all").text_content() == "Collapse all"
    tab.locator("#fold-all").click()
    tab.reload()
    assert opened(tab) == ""
    assert tab.locator("#fold-all").text_content() == "Expand all"


def test_default_order_is_activity_newest_first_and_every_sortable_header_shows_an_arrow(tab):
    assert order(tab) == "gbdaecf"
    assert arrows(tab) == {"kind": "↕", "open": "↕", "done": "↕", "swarm": "↕", "at": "▼"}


def test_a_header_click_sorts_by_it_and_a_second_click_reverses(tab):
    tab.locator(".sort[data-sort=done]").click()
    assert order(tab) == "cgfedab"
    assert arrows(tab)["done"] == "▼"
    assert arrows(tab)["at"] == "↕"
    tab.locator(".sort[data-sort=done]").click()
    assert order(tab) == "badefgc"
    assert arrows(tab)["done"] == "▲"


def test_open_and_kind_sort(tab):
    tab.locator(".sort[data-sort=open]").click()
    assert order(tab) == "bfeagcd"
    tab.locator(".sort[data-sort=kind]").click()
    assert order(tab) == "bdgaecf"


def test_swarm_sort_runs_running_paused_drained_stopped_none_closed(tab):
    tab.locator(".sort[data-sort=swarm]").click()
    assert order(tab) == "agdefbc"
    assert arrows(tab)["swarm"] == "▲"


def test_the_sort_choice_is_remembered_across_reloads(tab):
    tab.locator(".sort[data-sort=swarm]").click()
    tab.locator(".sort[data-sort=swarm]").click()
    tab.reload()
    assert order(tab) == "cbfegda"
    assert arrows(tab)["swarm"] == "▼"


def test_a_corrupt_stored_sort_falls_back_to_activity(context):
    context.add_init_script("""localStorage.setItem("home-sort", JSON.stringify({ key: "nope", dir: 7 }));""")
    tab = context.new_page()
    tab.goto(URL)
    assert order(tab) == "gbdaecf"


def test_blocked_storage_still_folds_and_sorts(context):
    context.add_init_script(BLOCKED_STORAGE)
    tab = context.new_page()
    tab.goto(URL)
    assert order(tab) == "gbdaecf"
    tab.locator("#fold-all").click()
    assert len(opened(tab)) == len(LEDGERS)
    tab.locator(".sort[data-sort=open]").click()
    assert order(tab) == "bfeagcd"
