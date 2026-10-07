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
from scripts.swarm_ledger.repository.file import FileLedgerRepository

storage = FileLedgerRepository(core)

MINUTE = 60 * 1000
SWARM_STATE = server.swarm_state
SLUG = "rows-2026-01-03"


def rule(selector):
    return re.search(r"(?:^|})" + re.escape(selector) + r"\{([^}]*)\}", home_style()).group(1)


def home_style():
    page = server.HOME_PAGE.read_text(encoding="utf-8")
    return re.search(r"<style>__HOME_PALETTE__(.*?)</style>", page, re.S).group(1).replace("\n", "")


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
    state, rejected = storage.apply_ops(SLUG, ops=ops)
    assert rejected == []
    return state["_meta"]["updated_at"]


@pytest.fixture(autouse=True)
def states():
    ledger_bin.bin_path().unlink(missing_ok=True)
    with patch.object(server, "swarm_state", side_effect=lambda slug: "running" if slug == SLUG else None):
        yield


def row(page, slug):
    return re.search(rf'<li class="row"[^>]*>(?:(?!</li>).)*href="/{slug}"(?:(?!</li>).)*</li>', page, re.S).group(0)


def summary(slug):
    return {s["slug"]: s for s in storage.list_summaries()}[slug]


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
    storage.apply_ops("phases-only", changes=[{"path": "phases/p1/done", "value": True}])
    found = summary("phases-only")
    assert (found["open"], found["done"], found["size"]) == (1, 1, "small")


def test_a_ledger_without_a_swarm_says_so_in_its_row(updated_at):
    make("no-swarm")
    found = row(server.index_page(), "no-swarm")
    assert '<span class="state s-none">no swarm</span>' in found


def test_a_closed_ledger_shows_closed_and_a_reopen_button(updated_at):
    make("closed-row")
    storage.apply_ops("closed-row", ops=[{"op": "close", "id": "c1", "by": "swarm"}])
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


def test_the_header_brands_home_without_a_ledger_count_and_names_each_column(updated_at):
    make("header-a")
    make("header-b")
    page = server.index_page()
    count = len(server.ledger_summaries())
    assert count > 1
    assert (
        '<header><span class="logo" aria-hidden="true"></span><span class="brand">agentihooks</span><h1>HOME</h1>'
        f"{server.FOLD_ALL}</header>" in page
    )
    assert 'class="total"' not in page
    head = re.search(r'<div class="row head">(.*?)</div>', page).group(1)
    arrow = '<i aria-hidden="true">&#8597;</i></button>'
    assert head == (
        f'<span></span><span>Ledger</span><button class="sort" type="button" data-sort="kind">Kind{arrow}'
        f'<span>Overview</span><button class="sort r" type="button" data-sort="open">Open{arrow}'
        f'<button class="sort r" type="button" data-sort="done">Done{arrow}'
        f'<button class="sort" type="button" data-sort="swarm">Swarm{arrow}'
        f'<button class="sort r" type="button" data-sort="at">Activity{arrow}<span></span>'
    )
    assert '<main class="home">' in page
    assert '</li><li class="row" data-slug=' in page


def test_the_bin_lists_days_left_and_a_restore_button(updated_at):
    ledger_bin.delete(SLUG, now=updated_at)
    assert f'href="/{SLUG}"' not in server.index_page()
    page = server.index_page(view="bin", now=updated_at + 29 * 24 * 60 * MINUTE)
    found = row(page, SLUG)
    date = server.time.strftime("%Y-%m-%d", server.time.localtime(updated_at / 1000))
    assert f'<span class="deleted">{date}</span><span class="left">1 day left</span>' in found
    assert f'<button class="act restore" type="button" data-act="restore" data-slug="{SLUG}"' in found
    assert "Restore</button>" in found
    assert (
        '<main class="bin"><header><span class="logo" aria-hidden="true"></span><span class="brand">agentihooks</span><h1>BIN</h1><span class="total">1 ledger</span></header>'
        in page
    )
    assert 'class="watermark"' not in page
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


class Swarms:
    def __init__(self, known=(), error=None):
        self.known, self.error = set(known), error

    def config(self, slug):
        if self.error:
            raise self.error
        if slug not in self.known:
            raise SwarmError(f"no swarm {slug}")
        return type("Config", (), {"state": "stopping"})()


def closed(slug):
    make(slug)
    storage.apply_ops(slug, ops=[{"op": "close", "id": "c1", "by": "swarm"}])
    return storage.apply_ops(slug)[0]["closed_at"]


def test_a_closed_ledger_without_a_swarm_goes_to_the_bin():
    closed_at = closed("orphan-closed")
    make("open-one")
    with patch.object(server, "swarm_store", return_value=Swarms()):
        server.bin_closed_without_swarm(now=closed_at + 5)
    assert ledger_bin.entries()["orphan-closed"] == closed_at + 5
    assert "open-one" not in ledger_bin.entries()


def test_a_closed_ledger_whose_swarm_is_still_stopping_stays_for_the_tick():
    closed("still-stopping")
    with patch.object(server, "swarm_store", return_value=Swarms({"still-stopping"})):
        server.bin_closed_without_swarm()
    assert "still-stopping" not in ledger_bin.entries()


def test_a_ledger_restored_then_closed_again_goes_back_to_the_bin():
    make("closed-twice")
    ledger_bin.delete("closed-twice", now=1)
    ledger_bin.restore("closed-twice", now=2)
    closed_at = closed("closed-twice")
    with patch.object(server, "swarm_store", return_value=Swarms()):
        server.bin_closed_without_swarm(now=closed_at + 1)
    assert ledger_bin.entries()["closed-twice"] == closed_at + 1


def test_an_unreachable_swarm_store_bins_nothing():
    closed("unreachable")
    with patch.object(server, "swarm_store", side_effect=SwarmError("Redis is unreachable")):
        server.bin_closed_without_swarm()
    with (
        patch.object(server, "swarm_store", return_value=Swarms(error=ConnectionError("refused"))),
        patch.object(server.sys, "stderr") as err,
    ):
        server.bin_closed_without_swarm()
    err.write.assert_called_once_with("bin closed ledgers: refused\n")
    assert "unreachable" not in ledger_bin.entries()


def test_home_bins_closed_ledgers_without_a_swarm_before_it_renders():
    closed("served-closed")
    handler = server.Handler.__new__(server.Handler)
    handler.path, handler.headers = "/", {"Host": f"127.0.0.1:{server.PORT}"}
    sent = []
    handler.send = lambda code, body, ctype: sent.append((code, body))
    with patch.object(server, "swarm_store", return_value=Swarms()):
        handler.do_GET()
    assert sent[0][0] == 200
    assert 'href="/served-closed"' not in sent[0][1]
    assert "served-closed" in ledger_bin.entries()
