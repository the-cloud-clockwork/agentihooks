import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"))
import ledger_bin  # noqa: E402
import ledger_core as core  # noqa: E402
import ledger_server as server  # noqa: E402
import new_ledger  # noqa: E402

from scripts.swarm.store import SwarmError  # noqa: E402

MINUTE = 60 * 1000
SWARM_STATE = server.swarm_state
SLUG = "rows-2026-01-03"


def rule(selector):
    return re.search(r"(?:^|})" + re.escape(selector) + r"\{([^}]*)\}", server.HOME_STYLE).group(1)


def make(slug, title="T", overview="O", size="swarm", phases=()):
    content = {"title": title, "overview": overview, "sources": [], "phases": list(phases), "questions": []}
    content["followups"] = []
    core.paths(slug)[0].write_text(new_ledger.render(new_ledger.build_doc(content, size), slug, 8765), encoding="utf-8")


def task_op(n, **fields):
    op = {
        "op": "task_add",
        "id": f"add-{n}",
        "by": "row-engineer",
        "task": f"t{n}",
        "title": f"Task {n}",
        "lane": "eng",
    }
    return {**op, **fields}


def update(n, fields):
    return {"op": "task_update", "id": f"up-{n}", "by": "row-engineer", "item": f"tasks/t{n}", "fields": fields}


@pytest.fixture(scope="module")
def updated_at():
    core.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    make(SLUG, "Rows plan", "A long overview " * 20)
    ops = [task_op(n) for n in (1, 2, 3, 4)] + [update(3, {"state": "done"}), update(4, {"out_of_scope": True})]
    state, rejected = core.sync(SLUG, ops=ops)
    assert rejected == []
    return state["_meta"]["updated_at"]


@pytest.fixture(autouse=True)
def states():
    ledger_bin.bin_path().unlink(missing_ok=True)
    with patch.object(server, "swarm_state", side_effect=lambda slug: "running" if slug == SLUG else None):
        yield


def row(page, slug):
    return re.search(rf'<li class="row">(?:(?!</li>).)*href="/{slug}"(?:(?!</li>).)*</li>', page, re.S).group(0)


def summary(slug):
    return {s["slug"]: s for s in server.all_summaries()}[slug]


def test_home_spans_the_full_window_width():
    assert "max-width" not in rule("main")
    assert "max-width" not in rule("ul")


def test_each_row_carries_title_kind_counts_swarm_state_and_last_activity(updated_at):
    found = row(server.index_page(now=updated_at + 5 * MINUTE), SLUG)
    assert '<a class="title" href="/rows-2026-01-03" title="Rows plan">Rows plan</a>' in found
    assert '<span class="kind">swarm</span>' in found
    assert '<span class="num open"><b>2</b> open</span><span class="num done"><b>1</b> done</span>' in found
    assert '<span class="state s-running">running</span>' in found
    assert re.search(r'<time class="when" datetime="[^"]+" title="[^"]+">5m ago</time>', found)
    assert re.search(r'<span class="ov" title="A long overview[^"]*">A long overview', found)
    assert '<span class="acts"><button class="act del" type="button" data-act="delete"' in found
    assert 'data-slug="rows-2026-01-03" title="Move to the bin" aria-label="Move Rows plan to the bin">' in found
    assert 'data-act="reopen"' not in found


def test_counts_skip_out_of_scope_tasks_and_activity_is_the_last_change(updated_at):
    found = summary(SLUG)
    assert (found["open"], found["done"], found["updated_at"]) == (2, 1, updated_at)


def test_a_ledger_without_tasks_counts_its_phases():
    make("phases-only", size="small", phases=[{"title": "a"}, {"title": "b"}])
    core.sync("phases-only", changes=[{"path": "phases/p1/done", "value": True}])
    found = summary("phases-only")
    assert (found["open"], found["done"], found["size"]) == (1, 1, "small")


def test_a_ledger_without_a_swarm_says_so_in_its_row(updated_at):
    make("no-swarm")
    found = row(server.index_page(), "no-swarm")
    assert '<span class="state s-none">no swarm</span>' in found


def test_a_closed_ledger_shows_closed_and_a_reopen_button(updated_at):
    make("closed-row")
    core.sync("closed-row", ops=[{"op": "close", "id": "c1", "by": "swarm"}])
    found = row(server.index_page(), "closed-row")
    assert '<span class="state s-closed">closed</span>' in found
    assert '<span class="acts"><button class="act reopen"' in found
    assert '>Reopen</button><button class="act del"' in found


def test_a_row_stays_on_one_line_and_truncates_the_overview():
    assert "grid-template-columns" in rule(".row")
    assert "white-space:nowrap" in rule(".row")
    for declaration in ("overflow:hidden", "text-overflow:ellipsis", "white-space:nowrap"):
        assert declaration in rule(".ov")


def test_buttons_and_the_floating_bin_entry_are_flat_at_rest():
    for selector in (".act", ".fab"):
        assert "background:transparent" in rule(selector)
        assert "border:0" in rule(selector)


def test_the_header_names_each_column_and_counts_the_ledgers(updated_at):
    page = server.index_page()
    count = len(server.ledger_summaries())
    assert f'<header><h1>HOME</h1><span class="total">{count} ledgers</span></header>' in page
    head = re.search(r'<div class="row head" aria-hidden="true">(.*?)</div>', page).group(1)
    assert head == (
        '<span>Ledger</span><span>Kind</span><span>Overview</span><span class="r">Open</span>'
        '<span class="r">Done</span><span>Swarm</span><span class="r">Activity</span><span></span>'
    )
    assert '<main class="home">' in page
    assert '</li><li class="row">' in page


def test_the_bin_lists_days_left_and_a_restore_button(updated_at):
    ledger_bin.delete(SLUG, now=updated_at)
    assert f'href="/{SLUG}"' not in server.index_page()
    page = server.index_page(view="bin", now=updated_at + 29 * 24 * 60 * MINUTE)
    found = row(page, SLUG)
    date = server.time.strftime("%Y-%m-%d", server.time.localtime(updated_at / 1000))
    assert f'<span class="deleted">{date}</span><span class="left">1 day left</span>' in found
    assert f'<button class="act restore" type="button" data-act="restore" data-slug="{SLUG}"' in found
    assert "Restore</button>" in found
    assert '<main class="bin"><header><h1>BIN</h1><span class="total">1 ledger</span></header>' in page
    assert '<span class="r">Deleted</span><span class="r">Left</span><span></span></div>' in page
    assert 'id="home-fab" href="/"' in page
    assert server.bin_cells({"deleted_at": updated_at, "days_left": 30}).endswith(">30 days left</span>")


def test_the_bin_entry_carries_the_bin_count_and_an_empty_bin_says_so(updated_at):
    assert '<span class="count">' not in server.index_page()
    assert '<li class="empty">The bin is empty.</li>' in server.index_page(view="bin")
    ledger_bin.delete(SLUG)
    assert '<span class="count">1</span></a>' in server.index_page()


@pytest.mark.parametrize(
    ("minutes", "text"),
    [(0, "just now"), (0.9, "just now"), (1, "1m ago"), (59, "59m ago"), (60, "1h ago"), (1439, "23h ago")]
    + [(1440, "1d ago"), (3000, "2d ago")],
)
def test_last_activity_reads_in_the_largest_whole_unit(minutes, text):
    assert server.ago(1000, 1000 + int(minutes * MINUTE)) == text


def test_activity_in_the_future_reads_just_now_and_unknown_when_missing():
    assert server.ago(5 * MINUTE, 0) == "just now"
    assert server.activity(None, 0) == '<span class="when">unknown</span>'
    stamp = server.activity(MINUTE, 3 * MINUTE)
    assert stamp.startswith('<time class="when" datetime="1970-01-01T00:01:00Z" title="1970-01-01 ')
    assert stamp.endswith('">2m ago</time>')
    at = 1_791_290_000_000
    local = server.time.strftime("%Y-%m-%d %H:%M", server.time.localtime(at / 1000))
    assert f'title="{local}">just now</time>' in server.activity(at, at)


def test_the_swarm_state_comes_from_the_swarm_config():
    class Store:
        def __init__(self, error=None):
            self.error = error

        def config(self, slug):
            assert slug == "sw"
            if self.error:
                raise self.error
            return type("Config", (), {"state": "paused"})()

    with patch.object(server, "swarm_store", return_value=Store()):
        assert SWARM_STATE("sw") == "paused"
    with patch.object(server, "swarm_store", return_value=Store(SwarmError("no swarm sw"))):
        assert SWARM_STATE("sw") is None
    with (
        patch.object(server, "swarm_store", return_value=Store(ConnectionError("refused"))),
        patch.object(server.sys, "stderr") as err,
    ):
        assert SWARM_STATE("sw") is None
    err.write.assert_called_once_with("swarm state sw: refused\n")


def test_home_cells_escape_an_unknown_state():
    cells = server.home_cells({"closed_at": None, "open": 0, "done": 0, "updated_at": None}, "<b>", 0)
    assert '<span class="state s-&lt;b&gt;">&lt;b&gt;</span>' in cells
