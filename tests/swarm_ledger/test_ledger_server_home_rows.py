import contextlib
import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"))

from scripts.swarm_ledger import ledger_server as server  # noqa: E402
from tests.swarm_ledger.ledger_page import chromium, rendered_home, served  # noqa: E402

URL = "/"
NOW = 700 * 60000
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


@contextlib.contextmanager
def listed(rows=None, states=None):
    states = states or {slug: spec[3] for slug, spec in LEDGERS.items()}
    with (
        patch.object(server, "ledger_summaries", return_value=rows or summaries()),
        patch.object(server, "bin_summaries", return_value=[{**summaries()[0], "deleted_at": 0, "days_left": 3}]),
        patch.object(server, "swarm_state", side_effect=lambda slug: states.get(slug)),
        patch.object(server.ledger_bin, "entries", return_value={}),
    ):
        yield


@pytest.fixture(scope="module")
def browser():
    with chromium() as launched:
        yield launched


@pytest.fixture(scope="module")
def home_html(browser):
    pages = {}

    def render(view="home"):
        if view not in pages:
            with listed():
                pages[view] = rendered_home(server, browser, view, NOW)
        return pages[view]

    return render


def row_attrs(page):
    return {m.group(1): m.group(0) for m in re.finditer(r'<li class="row" data-slug="(\w)"[^>]*>', page)}


def test_home_rows_carry_the_values_their_columns_sort_by(home_html):
    rows = row_attrs(home_html())
    assert set(rows) == set(LEDGERS)
    assert 'data-kind="small"' in rows["b"]
    assert 'data-open="9"' in rows["b"]
    assert 'data-done="7"' in rows["c"]
    assert f'data-at="{600 * 60000}"' in rows["g"]


@pytest.mark.parametrize(("slug", "rank"), [("a", 0), ("d", 1), ("g", 1), ("e", 2), ("f", 3), ("b", 4), ("c", 5)])
def test_swarm_rank_runs_running_paused_drained_stopped_none_closed(home_html, slug, rank):
    assert f'data-swarm="{rank}"' in row_attrs(home_html())[slug]


def test_unknown_swarm_state_ranks_with_no_swarm(browser):
    with listed(states={"a": "rebooting"}):
        rows = row_attrs(rendered_home(server, browser, "home", NOW))
    assert 'data-swarm="4"' in rows["a"]
    assert 'data-swarm="4"' in rows["b"]


def test_exactly_kind_open_done_swarm_and_activity_headers_sort(home_html):
    page = home_html()
    assert re.findall(r'data-sort="(\w+)"', page) == ["kind", "open", "done", "swarm", "at"]
    assert page.count('class="fold"') == len(LEDGERS)
    assert 'id="fold-all"' in page


def test_a_ledger_without_activity_sorts_as_oldest(browser):
    with listed(rows=[{**summaries()[0], "updated_at": None}]):
        page = rendered_home(server, browser, "home", NOW)
    assert ' data-at="0">' in page


def test_the_fold_arrow_names_its_ledger_escaped(browser):
    with listed(rows=[{**summaries()[1], "title": "Beta <plan><img src=x>"}]):
        page = rendered_home(server, browser, "home", NOW)
    assert "<img" not in page.split('<ul id="rows">', 1)[1]
    assert 'aria-label="Show all of Beta &lt;plan&gt;&lt;img src=x&gt;"' in page


def test_bin_has_no_fold_or_sort_controls(home_html):
    page = home_html("bin")
    assert '<li class="row"><a class="title" href="/a"' in page
    assert "data-sort=" not in page
    assert 'class="fold"' not in page
    assert 'id="fold-all"' not in page


@pytest.fixture
def context(browser):
    with listed(), served(server) as base:
        context = browser.new_context(viewport={"width": 1920, "height": 1080}, base_url=base)
        context.set_default_timeout(5000)
        context.add_init_script(f"Date.now = () => {NOW};")
        yield context
        context.close()


def opened_page(context):
    page = context.new_page()
    page.goto(URL)
    page.wait_for_selector("li.row")
    return page


@pytest.fixture
def tab(context):
    return opened_page(context)


def reload(tab):
    tab.reload()
    tab.wait_for_selector("li.row")


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


def test_home_side_margins_are_192_pixels_each_on_a_wide_screen(tab):
    pads = tab.evaluate(
        "() => { const s = getComputedStyle(document.querySelector('main')); return [s.paddingLeft, s.paddingRight]; }"
    )
    assert pads == ["192px", "192px"]


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
    reload(tab)
    assert opened(tab) == "b"
    tab.locator("#fold-all").click()
    assert len(opened(tab)) == len(LEDGERS)
    assert tab.locator("#fold-all").text_content() == "Collapse all"
    reload(tab)
    assert len(opened(tab)) == len(LEDGERS)
    assert tab.locator("#fold-all").text_content() == "Collapse all"
    tab.locator("#fold-all").click()
    reload(tab)
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
    reload(tab)
    assert order(tab) == "cbfegda"
    assert arrows(tab)["swarm"] == "▼"


def test_a_corrupt_stored_sort_falls_back_to_activity(context):
    context.add_init_script("""localStorage.setItem("home-sort", JSON.stringify({ key: "nope", dir: 7 }));""")
    tab = opened_page(context)
    assert order(tab) == "gbdaecf"


def test_blocked_storage_still_folds_and_sorts(context):
    context.add_init_script(BLOCKED_STORAGE)
    tab = opened_page(context)
    assert order(tab) == "gbdaecf"
    tab.locator("#fold-all").click()
    assert len(opened(tab)) == len(LEDGERS)
    tab.locator(".sort[data-sort=open]").click()
    assert order(tab) == "bfeagcd"
