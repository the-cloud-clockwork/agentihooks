import inspect
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"))
import ledger_bin  # noqa: E402
import ledger_core as core  # noqa: E402
import ledger_server as server  # noqa: E402
import new_ledger  # noqa: E402

from scripts.swarm.store import SwarmError  # noqa: E402
from scripts.swarm_ledger.storage import default as default_repository  # noqa: E402
from tests.swarm_ledger import legacy_page  # noqa: E402
from tests.swarm_ledger.ledger_page import chromium, rendered_home  # noqa: E402

storage = default_repository()

MINUTE = 60 * 1000
SWARM_STATE = server.swarm_state
SLUG = "rows-2026-01-03"


def rule(selector):
    return re.search(r"(?:^|})" + re.escape(selector) + r"\{([^}]*)\}", home_style()).group(1)


def home_style():
    return core.static_assets()["css/home.css"].read_text(encoding="utf-8").replace("\n", "")


@pytest.fixture(scope="module")
def browser():
    with chromium() as launched:
        yield launched


def home(browser, view="home", now=None):
    return rendered_home(server, browser, view, now)


def one_row(browser, now, **fields):
    s = {"slug": "one", "title": "One", "overview": "o", "size": "swarm", "open": 0, "done": 0, "closed_at": None}
    with patch.object(server, "ledger_summaries", return_value=[{**s, **fields}]):
        return home(browser, now=now)


def make(slug, title="T", overview="O", size="swarm", phases=()):
    content = {"title": title, "overview": overview, "sources": [], "phases": list(phases), "questions": []}
    content["followups"] = []
    legacy_page.paths(slug, core)[0].write_text(
        legacy_page.render(new_ledger.build_doc(content, size), slug, 8765), encoding="utf-8"
    )


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
    with storage.connect() as connection, connection:
        storage.save_registry(connection, "bin", {})
    with patch.object(server, "swarm_state", side_effect=lambda slug: "running" if slug == SLUG else None):
        yield


def row(page, slug):
    return re.search(rf'<li class="row"[^>]*>(?:(?!</li>).)*href="/{slug}"(?:(?!</li>).)*</li>', page, re.S).group(0)


def summary(slug):
    return {s["slug"]: s for s in storage.list_summaries()}[slug]


def test_home_spans_the_full_window_width():
    assert "max-width" not in rule("main")
    assert "max-width" not in rule("ul")


def test_each_row_carries_title_kind_counts_swarm_state_and_last_activity(browser, updated_at):
    found = row(home(browser, now=updated_at + 5 * MINUTE), SLUG)
    assert (
        '<span class="title" role="cell"><a href="/rows-2026-01-03" data-tip="Rows plan">Rows plan</a></span>' in found
    )
    assert '<span class="kind" role="cell">swarm</span>' in found
    assert (
        '<span class="num open" role="cell"><b>2</b> open</span><span class="num done" role="cell"><b>1</b> done</span>'
        in found
    )
    assert '<span class="state s-running" role="cell">running</span>' in found
    assert re.search(r'<time class="when" datetime="[^"]+" data-tip="[^"]+" role="cell">5m ago</time>', found)
    assert re.search(
        r'</a></span><span class="kind" role="cell">swarm</span><span class="ov" data-tip="A long overview[^"]*" role="cell">A long overview[^<]*</span>'
        r'<span class="num open" role="cell">',
        found,
    )
    assert '<span class="acts" role="cell"><button class="act del" type="button" data-act="delete"' in found
    assert 'data-slug="rows-2026-01-03" aria-label="Move Rows plan to the bin">' in found
    assert 'data-act="reopen"' not in found


def test_counts_skip_out_of_scope_tasks_and_activity_is_the_last_change(updated_at):
    found = summary(SLUG)
    assert (found["open"], found["done"], found["updated_at"]) == (2, 1, updated_at)


def test_a_ledger_without_tasks_counts_its_phases():
    make("phases-only", size="small", phases=[{"title": "a"}, {"title": "b"}])
    storage.apply_ops("phases-only", changes=[{"path": "phases/p1/done", "value": True}])
    found = summary("phases-only")
    assert (found["open"], found["done"], found["size"]) == (1, 1, "small")


def test_a_ledger_without_a_swarm_says_so_in_its_row(browser, updated_at):
    make("no-swarm")
    found = row(home(browser), "no-swarm")
    assert '<span class="state s-none" role="cell">no swarm</span>' in found


def test_a_closed_ledger_shows_closed_and_a_reopen_button(browser, updated_at):
    make("closed-row")
    storage.apply_ops("closed-row", ops=[{"op": "close", "id": "c1", "by": "swarm"}])
    found = row(home(browser), "closed-row")
    assert '<span class="state s-closed" role="cell">closed</span>' in found
    assert '<span class="acts" role="cell"><button class="act reopen"' in found
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


def test_the_header_brands_home_without_a_ledger_count_and_names_each_column(browser, updated_at):
    make("header-a")
    make("header-b")
    page = home(browser)
    count = len(server.ledger_summaries())
    assert count > 1
    assert (
        '<header><a class="logo-link" href="/" aria-label="HOME"><span class="logo" aria-hidden="true"></span></a><span class="brand">agentihooks</span><h1>HOME</h1>'
        "</header>" in page
    )
    assert 'class="total"' not in page
    assert 'id="fold-all"' not in page
    head = re.search(
        r'<div class="row head" role="row">(.*?)</div>',
        re.sub(r'<span class="col-grip"[^>]*></span>', "", page),
    ).group(1)
    cells = re.findall(r'<span role="columnheader"[^>]*>(.*?)</span>', head)
    labels = [re.sub(r"<[^>]+>|[↕▲▼]", "", cell) for cell in cells]
    assert labels == ["Ledger", "Kind", "Overview", "Open", "Done", "Swarm", "Activity", "Actions"]
    assert re.findall(r'data-sort="(\w+)"', head) == ["kind", "open", "done", "swarm", "at"]
    assert head.count('aria-sort="none"') == 4
    assert 'aria-sort="descending"' in head
    assert '<i aria-hidden="true">▼</i></button>' in head
    assert head.count('<i aria-hidden="true">↕</i></button>') == 4
    assert '<main class="home">' in page
    assert '</li><li class="row" role="row" data-slug=' in page


def test_the_bin_lists_days_left_and_a_restore_button(browser, updated_at):
    ledger_bin.delete(SLUG, now=updated_at)
    assert f'href="/{SLUG}"' not in home(browser)
    with patch.object(server.core, "now_ms", return_value=updated_at + 29 * 24 * 60 * MINUTE):
        page = home(browser, "bin")
    found = row(page, SLUG)
    date = server.time.strftime("%Y-%m-%d", server.time.localtime(updated_at / 1000))
    assert f'<span class="deleted" role="cell">{date}</span><span class="left" role="cell">1 day left</span>' in found
    assert f'<button class="act restore" type="button" data-act="restore" data-slug="{SLUG}"' in found
    assert "Restore</button>" in found
    assert (
        '<main class="bin"><header><a class="logo-link" href="/" aria-label="HOME"><span class="logo" aria-hidden="true"></span></a><span class="brand">agentihooks</span><h1>BIN</h1><span class="total" id="total">1 ledger</span></header>'
        in page
    )
    assert 'class="watermark"' not in page
    head = re.sub(r'<span class="col-grip"[^>]*></span>', "", page)
    assert (
        '<span role="columnheader" class="r">Deleted</span><span role="columnheader" class="r">Left</span><span role="columnheader" class="r">Actions</span></div>'
        in head
    )
    assert 'id="home-fab" href="/"' in page
    with patch.object(
        server, "bin_summaries", return_value=[{**summary(SLUG), "deleted_at": updated_at, "days_left": 30}]
    ):
        assert '<span class="left" role="cell">30 days left</span>' in row(home(browser, "bin"), SLUG)


def test_the_bin_entry_carries_the_bin_count_and_an_empty_bin_says_so(browser, updated_at):
    assert '<span class="count">' not in home(browser)
    assert '<li class="empty" role="row"><span role="cell" aria-colspan="6">The bin is empty.' in home(browser, "bin")
    ledger_bin.delete(SLUG)
    assert '<span class="count">1</span></a>' in home(browser)


@pytest.mark.parametrize(
    ("minutes", "text"),
    [(0, "just now"), (0.9, "just now"), (1, "1m ago"), (59, "59m ago"), (60, "1h ago"), (1439, "23h ago")]
    + [(1440, "1d ago"), (3000, "2d ago")],
)
def test_last_activity_reads_in_the_largest_whole_unit(browser, minutes, text):
    assert f">{text}</time>" in one_row(browser, 1000 + int(minutes * MINUTE), updated_at=1000)


def test_activity_in_the_future_reads_just_now_and_unknown_when_missing(browser):
    assert ">just now</time>" in one_row(browser, 0, updated_at=5 * MINUTE)
    assert '<span class="when" role="cell">unknown</span>' in one_row(browser, 0, updated_at=None)
    stamp = re.search(r'<time class="when"[^>]*>[^<]*</time>', one_row(browser, 3 * MINUTE, updated_at=MINUTE)).group(0)
    assert stamp.startswith('<time class="when" datetime="1970-01-01T00:01:00Z" data-tip="1970-01-01 ')
    assert stamp.endswith('" role="cell">2m ago</time>')
    at = 1_791_290_000_000
    local = server.time.strftime("%Y-%m-%d %H:%M", server.time.localtime(at / 1000))
    assert f'data-tip="{local}" role="cell">just now</time>' in one_row(browser, at, updated_at=at)


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


def test_home_cells_escape_an_unknown_state(browser):
    with patch.object(server, "swarm_state", return_value="<b>"):
        page = one_row(browser, 0, updated_at=None)
    assert re.search(r'<span class="state s-(&lt;|<)b(&gt;|>)" role="cell">&lt;b&gt;</span>', page)


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


def test_the_home_shell_bins_nothing_and_the_watch_loop_bins_closed_ledgers_without_a_swarm():
    closed("served-closed")
    handler = server.Handler.__new__(server.Handler)
    handler.path, handler.headers = "/", {"Host": f"127.0.0.1:{server.PORT}"}
    sent = []
    handler.send = lambda code, body, ctype: sent.append((code, body))
    with patch.object(server, "swarm_store", return_value=Swarms()):
        handler.do_GET()
    assert sent[0][0] == 200
    assert "served-closed" not in ledger_bin.entries()

    def finish(interval):
        raise RuntimeError("watch ended")

    with (
        patch.object(server, "swarm_store", return_value=Swarms()),
        patch.object(server, "sample_streams"),
        patch.object(server, "scheduled_wait", finish),
        pytest.raises(RuntimeError, match="watch ended"),
    ):
        server.watch_ledgers()
    assert "served-closed" in ledger_bin.entries()


def unbound_server():
    return server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler, bind_and_activate=False)


def test_draining_waits_for_the_requests_in_flight():
    import threading

    httpd = unbound_server()
    try:
        assert httpd.drained(0.05) is True
        with httpd.idle:
            httpd.active = 1

        def finish():
            with httpd.idle:
                httpd.active = 0
                httpd.idle.notify_all()

        timer = threading.Timer(1.0, finish)
        timer.start()
        assert httpd.drained(0.05) is False
        assert httpd.drained(10) is True
        timer.join()
    finally:
        httpd.server_close()


def test_a_request_counts_as_in_flight_from_accept_until_its_thread_ends():
    httpd = unbound_server()
    started = []
    httpd.process_request_thread = lambda request, address: started.append((request, address, httpd.active))
    httpd.active = 2
    httpd.process_request("request", ("127.0.0.1", 1))
    httpd.server_close()
    assert started == [("request", ("127.0.0.1", 1), 3)]
    assert httpd.active == 3


def test_the_recorded_pid_is_empty_without_a_pidfile(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "PIDFILE", tmp_path / ".server.pid")
    assert server.recorded_pid() == ""
    server.PIDFILE.write_text("41")
    assert server.recorded_pid() == "41"


def test_a_finished_request_thread_leaves_flight_and_wakes_the_drain():
    httpd = unbound_server()
    handled, woken = [], []
    httpd.finish_request = lambda request, address: handled.append((request, address, httpd.active))
    httpd.shutdown_request = lambda request: None
    httpd.active = 2
    httpd.idle.notify_all = lambda: woken.append(httpd.active)
    try:
        httpd.process_request_thread("request", ("127.0.0.1", 1))
        assert handled == [("request", ("127.0.0.1", 1), 2)]
        assert httpd.active == 1
        assert woken == [1]
    finally:
        httpd.server_close()


def test_a_request_thread_that_fails_still_leaves_flight():
    httpd = unbound_server()

    def broken(request, address):
        raise OSError("reset")

    httpd.finish_request = broken
    httpd.shutdown_request = lambda request: None
    httpd.handle_error = lambda request, address: None
    httpd.active = 1
    try:
        httpd.process_request_thread("request", ("127.0.0.1", 1))
        assert httpd.active == 0
    finally:
        httpd.server_close()


def test_an_inherited_socket_is_served_without_binding():
    import socket

    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        held.listen(8)
        httpd = server.listening(held)
        try:
            assert httpd.socket is held
            assert httpd.request_queue_size == 128
        finally:
            httpd.socket = None


def test_a_fresh_server_binds_its_own_port(monkeypatch):
    import socket

    with socket.socket() as spare:
        spare.bind(("127.0.0.1", 0))
        port = spare.getsockname()[1]
    monkeypatch.setattr(server, "HOST", "127.0.0.1")
    monkeypatch.setattr(server, "PORT", port)
    httpd = server.listening()
    try:
        assert httpd.socket.getsockname() == ("127.0.0.1", port)
        assert httpd.request_queue_size == 128
    finally:
        httpd.server_close()


def test_warming_loads_every_open_ledger_and_survives_a_broken_one(monkeypatch):
    loaded, entries = [], {"fine": object()}

    def current(slug):
        loaded.append(slug)
        if slug == "broken":
            raise ValueError("bad rows")
        return entries[slug]

    monkeypatch.setattr(server, "ledger_summaries", lambda: [{"slug": "broken"}, {"slug": "fine"}])
    monkeypatch.setattr(server.stored, "current", current)
    server.warm()
    assert loaded == ["broken", "fine", "broken", "fine"]


def test_warming_repeats_while_a_ledger_changed_up_to_its_passes(monkeypatch):
    loaded = []

    def current(slug):
        loaded.append(slug)
        return object() if slug == "busy" else "same"

    monkeypatch.setattr(server, "ledger_summaries", lambda: [{"slug": "busy"}, {"slug": "quiet"}])
    monkeypatch.setattr(server.stored, "current", current)
    server.warm(passes=3)
    assert loaded == ["busy", "quiet"] * 3


def test_warming_stops_after_one_pass_without_ledgers(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "ledger_summaries", lambda: calls.append("listed") or [])
    server.warm()
    assert calls == ["listed"]
    assert server.WARM_PASSES == 5


class Handed:
    socket = "socket"

    def shutdown(self):
        pass


def test_a_reload_names_the_watched_ledgers_to_its_successor(monkeypatch):
    seen = []
    monkeypatch.setattr(server, "HUB", SimpleNamespace(watched=lambda: ["demo", "other"]))
    monkeypatch.setattr(server, "HANDOFF", {})

    def hand_off(listener, argv, extra):
        seen.append((listener, extra))
        return b"{}"

    monkeypatch.setattr(server, "loaded_modules", lambda: "json,ledger_core")
    assert server.reload_if_changed(1, Handed(), (), hand_off, force=True) is None
    assert seen == [("socket", {server.WATCHED: "demo,other", server.MODULES: "json,ledger_core"})]


def test_a_reload_whose_successor_never_got_ready_says_so_and_keeps_serving(monkeypatch, capsys):
    monkeypatch.setattr(server, "HUB", SimpleNamespace(watched=lambda: []))
    started = server.code_stamp(())
    assert server.reload_if_changed(1, Handed(), (), lambda *args, **kwargs: None, force=True) == started
    assert capsys.readouterr().err == "reload: the successor never reported ready; this server keeps serving\n"


def test_a_reload_whose_successor_cannot_start_says_so_and_keeps_serving(monkeypatch, capsys):
    monkeypatch.setattr(server, "HUB", SimpleNamespace(watched=lambda: []))

    def refused(*args, **kwargs):
        raise OSError("no such interpreter")

    started = server.code_stamp(())
    assert server.reload_if_changed(1, Handed(), (), refused, force=True) == started
    assert capsys.readouterr().err == (
        "reload: the successor did not start: no such interpreter; this server keeps serving\n"
    )


def test_a_reload_with_an_unreadable_ready_payload_carries_every_ledger(monkeypatch):
    monkeypatch.setattr(server, "HUB", SimpleNamespace(watched=lambda: []))
    monkeypatch.setattr(server, "HANDOFF", {})
    assert server.reload_if_changed(1, Handed(), (), lambda *args, **kwargs: b"{broken", force=True) is None
    assert server.HANDOFF == {"since": {}}


def test_a_container_s_main_server_reloads_in_place(monkeypatch):
    ran = []
    monkeypatch.setattr(server, "container_main", lambda: True)
    monkeypatch.setattr(server, "metrics_journal", lambda step: ran.append(step))

    def execv(path, argv):
        ran.append((path, argv))
        raise SystemExit(0)

    monkeypatch.setattr(server.os, "execv", execv)
    with pytest.raises(SystemExit):
        server.reload_if_changed(1, Handed(), (), lambda *args, **kwargs: ran.append("handed"), force=True)
    assert ran == [
        server.request_metrics.stop,
        (sys.executable, [sys.executable, str(server.CODE_DIR / "ledger_server.py"), "--serve"]),
    ]


def test_a_container_s_main_server_that_cannot_restart_in_place_keeps_serving(monkeypatch, capsys):
    journal = []
    monkeypatch.setattr(server, "container_main", lambda: True)
    monkeypatch.setattr(server, "metrics_journal", lambda step, *args: journal.append((step, *args)))

    def execv(path, argv):
        raise OSError("exec format error")

    monkeypatch.setattr(server.os, "execv", execv)
    started = server.code_stamp(())
    handed = server.reload_if_changed(1, Handed(), (), lambda *args, **kwargs: pytest.fail("handed off"), force=True)
    assert handed == started
    assert capsys.readouterr().err == (
        "reload: could not restart in place: exec format error; this server keeps serving\n"
    )
    assert journal == [
        (server.request_metrics.stop,),
        (server.request_metrics.restore, server.core.LEDGER_DIR / server.request_metrics.JOURNAL_NAME),
    ]


@pytest.mark.parametrize(("comm", "main"), [("containerd-shim\n", True), ("python\n", False), (None, False)])
def test_a_server_whose_parent_is_the_container_shim_is_the_container_s_main_process(tmp_path, comm, main):
    parent = tmp_path / str(os.getppid())
    parent.mkdir()
    if comm is not None:
        (parent / "comm").write_text(comm)
    assert server.container_main(tmp_path) is main
    assert server.container_main() is (Path(f"/proc/{os.getppid()}/comm").read_text().startswith("containerd-shim"))


def test_a_hangup_asks_for_a_hand_off(monkeypatch):
    requested = server.threading.Event()
    monkeypatch.setattr(server, "RELOAD", requested)
    server.request_reload(1, None)
    assert requested.is_set()


class Requests:
    def __init__(self, left):
        self.left = left
        self.cleared = 0

    def wait(self):
        self.left -= 1
        if self.left < 0:
            raise StopIteration
        return True

    def clear(self):
        self.cleared += 1


def test_a_hangup_hands_off_on_its_own_thread_and_then_stops(monkeypatch):
    requested = Requests(1)
    seen = []
    monkeypatch.setattr(server, "HANDED", server.threading.Event())
    monkeypatch.setattr(server, "code_stamp", lambda: 7)

    def reload_if_changed(started, httpd, force, loaded):
        seen.append((started, httpd, force, loaded))

    monkeypatch.setattr(server, "reload_if_changed", reload_if_changed)
    assert server.answer_reloads("httpd", requested) is None
    assert seen == [(7, "httpd", True, server.LOADED_DIGEST)]
    assert requested.cleared == 1
    assert server.HANDED.is_set()


def test_the_hangup_thread_waits_on_the_reload_request_by_default():
    assert inspect.signature(server.answer_reloads).parameters["requested"].default is server.RELOAD


def test_a_hangup_the_successor_refused_waits_for_the_next_one(monkeypatch):
    requested = Requests(2)
    monkeypatch.setattr(server, "HANDED", server.threading.Event())
    monkeypatch.setattr(server, "code_stamp", lambda: 7)
    monkeypatch.setattr(server, "reload_if_changed", lambda started, httpd, force, loaded: started)
    with pytest.raises(StopIteration):
        server.answer_reloads("httpd", requested)
    assert requested.cleared == 2
    assert not server.HANDED.is_set()


def test_the_server_hands_over_only_once(monkeypatch):
    seen = []
    monkeypatch.setattr(server, "HANDED", server.threading.Event())
    monkeypatch.setattr(server, "reload_if_changed", lambda *args, **kwargs: seen.append((args, kwargs)))
    assert server.hand_over(5, "httpd", force=True) is None
    assert server.hand_over(5, "httpd") is None
    assert seen == [((5, "httpd"), {"force": True, "loaded": server.LOADED_DIGEST})]


def test_an_unchanged_check_keeps_the_server_and_its_next_stamp(monkeypatch):
    monkeypatch.setattr(server, "HANDED", server.threading.Event())
    monkeypatch.setattr(server, "reload_if_changed", lambda started, httpd, force, loaded: started + 1)
    assert server.hand_over(5, "httpd") == 6
    assert not server.HANDED.is_set()


def test_handing_over_holds_the_lock(monkeypatch):
    held = []
    monkeypatch.setattr(server, "HANDED", server.threading.Event())
    monkeypatch.setattr(server, "reload_if_changed", lambda *args, **kwargs: held.append(server.HANDING.locked()))
    server.hand_over(5, "httpd")
    assert held == [True]
    assert not server.HANDING.locked()


def test_the_watch_loop_stops_once_the_code_reload_handed_over(monkeypatch):
    seen = []
    monkeypatch.setattr(server, "HANDED", server.threading.Event())
    monkeypatch.setattr(server, "reloading", lambda: True)
    monkeypatch.setattr(server, "LOADED_STAMP", 7)
    monkeypatch.setattr(server, "reload_if_changed", lambda *args, **kwargs: seen.append((args, kwargs)))
    assert server.watch_ledgers("httpd") is None
    assert seen == [((7, "httpd"), {"force": False, "loaded": server.LOADED_DIGEST})]


def test_the_watch_loop_ends_once_its_server_stops(monkeypatch):
    stopping = server.threading.Event()
    stopping.set()
    monkeypatch.setattr(server, "reloading", lambda: False)
    monkeypatch.setattr(server.ledger_bin, "tidy", lambda: pytest.fail("tidied after the server stopped"))
    assert server.watch_ledgers(stopping=stopping) is None
    assert inspect.signature(server.watch_ledgers).parameters["stopping"].default is server.NEVER
    assert not server.NEVER.is_set()


def test_a_watch_pass_holds_the_pass_lock_but_its_wait_does_not(monkeypatch):
    held = []
    monkeypatch.setattr(server, "reloading", lambda: False)
    monkeypatch.setattr(server.ledger_bin, "tidy", lambda: held.append(server.WATCH_PASS.locked()))
    monkeypatch.setattr(server, "bin_closed_without_swarm", lambda: None)
    monkeypatch.setattr(server, "unrelayed", lambda: False)
    monkeypatch.setattr(server, "sample_streams", lambda: None)
    monkeypatch.setattr(server, "metrics_journal", lambda *args: None)

    def wait(interval):
        held.append(server.WATCH_PASS.locked())
        raise StopIteration

    monkeypatch.setattr(server, "scheduled_wait", wait)
    with pytest.raises(StopIteration):
        server.watch_ledgers()
    assert held == [True, False]


@pytest.fixture
def quick_clock(monkeypatch):
    ticks = iter(range(1_000_000))
    monkeypatch.setattr(server.time, "monotonic", lambda: float(next(ticks)))
    monkeypatch.setattr(server.time, "sleep", lambda seconds: None)


def test_reload_signals_the_running_server_and_waits_for_its_successor(tmp_path, monkeypatch, quick_clock):
    pidfile = tmp_path / ".server.pid"
    pidfile.write_text("41")
    sent, naps = [], []
    monkeypatch.setattr(server, "PIDFILE", pidfile)
    monkeypatch.setattr(server, "server_process_alive", lambda: True)
    monkeypatch.setattr(server, "ensure", lambda: sent.append("ensure"))
    monkeypatch.setattr(server.os, "kill", lambda pid, sig: sent.append((pid, sig)))

    def nap(seconds):
        naps.append(seconds)
        pidfile.write_text("42")

    monkeypatch.setattr(server.time, "sleep", nap)
    server.reload()
    assert sent == [(41, server.signal.SIGHUP), "ensure"]
    assert naps == [0.1]


@pytest.mark.parametrize(("pid", "kill"), [("", None), ("41", ProcessLookupError)])
def test_reload_starts_a_server_when_the_recorded_one_is_unreadable_or_gone(
    tmp_path, monkeypatch, pid, kill, quick_clock
):
    pidfile = tmp_path / ".server.pid"
    pidfile.write_text(pid)
    sent = []

    def signal_it(target, sig):
        sent.append((target, sig))
        raise kill()

    monkeypatch.setattr(server, "PIDFILE", pidfile)
    monkeypatch.setattr(server, "server_process_alive", lambda: True)
    monkeypatch.setattr(server, "ensure", lambda: "started")
    monkeypatch.setattr(server.os, "kill", signal_it)
    assert server.reload() == "started"
    assert sent == ([] if kill is None else [(41, server.signal.SIGHUP)])


def test_reload_starts_a_server_when_none_runs(monkeypatch, quick_clock):
    sent = []
    monkeypatch.setattr(server, "server_process_alive", lambda: False)
    monkeypatch.setattr(server, "ensure", lambda: sent.append("ensure"))
    monkeypatch.setattr(server.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    server.reload()
    assert sent == ["ensure"]


def test_reload_fails_when_the_server_never_hands_off(tmp_path, monkeypatch):
    pidfile = tmp_path / ".server.pid"
    pidfile.write_text("41")
    clock = iter([0.0, 1.0, 2.0])
    monkeypatch.setattr(server, "PIDFILE", pidfile)
    monkeypatch.setattr(server, "server_process_alive", lambda: True)
    monkeypatch.setattr(server.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(server.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(server.time, "sleep", lambda seconds: None)
    with pytest.raises(SystemExit, match="pid 41 did not hand off within 2 s"):
        server.reload(wait=2.0)


def test_reload_starts_a_server_when_the_signalled_one_dies_without_handing_off(tmp_path, monkeypatch, quick_clock):
    pidfile = tmp_path / ".server.pid"
    pidfile.write_text("41")
    alive, sent = iter([True, False]), []
    monkeypatch.setattr(server, "PIDFILE", pidfile)
    monkeypatch.setattr(server, "server_process_alive", lambda: next(alive))
    monkeypatch.setattr(server, "ensure", lambda: sent.append("ensure") or "started")
    monkeypatch.setattr(server.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(server.time, "sleep", lambda seconds: sent.append(("sleep", seconds)))
    assert server.reload() == "started"
    assert sent == [(41, server.signal.SIGHUP), "ensure"]


def test_reload_applies_the_compose_service_then_hands_off(tmp_path, monkeypatch, quick_clock):
    pidfile = tmp_path / ".server.pid"
    pidfile.write_text("41")
    sent, used = [], []
    monkeypatch.setattr(server, "PIDFILE", pidfile)
    monkeypatch.setattr(server.server_compose, "used", lambda folder: used.append(folder) or True)
    monkeypatch.setattr(server.server_compose, "start", lambda folder: sent.append(("start", folder)) or "")
    monkeypatch.setattr(server, "server_process_alive", lambda: True)
    monkeypatch.setattr(server, "ensure", lambda: sent.append("ensure") or "started")
    monkeypatch.setattr(server.os, "kill", lambda pid, sig: sent.append((pid, sig)) or pidfile.write_text("42"))
    assert server.reload() == "started"
    assert sent == [("start", server.core.LEDGER_DIR), (41, server.signal.SIGHUP), "ensure"]
    assert used == [server.core.LEDGER_DIR]


def test_reload_stops_when_the_compose_service_cannot_start(monkeypatch, quick_clock):
    monkeypatch.setattr(server.server_compose, "used", lambda folder: True)
    monkeypatch.setattr(server.server_compose, "start", lambda folder: "no such image")
    monkeypatch.setattr(server.os, "kill", lambda pid, sig: pytest.fail("signalled"))
    with pytest.raises(SystemExit, match="could not start the ledger service: no such image"):
        server.reload()


def test_the_reload_flag_runs_reload(monkeypatch):
    ran = []
    monkeypatch.setattr(server, "reload", lambda: ran.append("reload"))
    monkeypatch.setattr(sys, "argv", ["ledger_server.py", "--reload"])
    server.main()
    assert ran == ["reload"]


def test_serving_listens_for_a_hangup_before_it_serves(monkeypatch):
    seen = []
    monkeypatch.setattr(server.signal, "signal", lambda sig, handler: seen.append((sig, handler)))
    monkeypatch.setattr(server, "serve", lambda: seen.append("serve"))
    monkeypatch.setattr(sys, "argv", ["ledger_server.py", "--serve"])
    server.main()
    assert seen == [(server.signal.SIGHUP, server.request_reload), "serve"]


def test_a_carry_goes_from_the_old_server_to_its_successor_once(tmp_path, monkeypatch):
    folder = tmp_path / "ledgers"
    folder.mkdir()
    monkeypatch.setattr(server.core, "LEDGER_DIR", folder)
    monkeypatch.setattr(server.stored, "carry", lambda since: {"demo": [7, "{}"]} if since == {"demo": 6} else {})
    adopted = []
    monkeypatch.setattr(server.stored, "adopt_carried", adopted.append)
    server.write_carry({"demo": 6})
    assert sorted(p.name for p in folder.iterdir()) == [server.CARRY]
    assert server.take_carry() == ["demo"]
    assert server.take_carry() is None
    assert adopted == [{"demo": [7, "{}"]}]
    assert list(folder.iterdir()) == []


def test_a_broken_carry_is_dropped_and_the_successor_goes_on(tmp_path, monkeypatch, capsys):
    folder = tmp_path / "ledgers"
    folder.mkdir()
    monkeypatch.setattr(server.core, "LEDGER_DIR", folder)
    (folder / server.CARRY).write_text("{not json")
    monkeypatch.setattr(server.stored, "adopt_carried", lambda carried: pytest.fail("adopted a broken carry"))
    assert server.take_carry() is None
    assert list(folder.iterdir()) == []
    assert capsys.readouterr().err.startswith("carry: ")


def test_a_carry_the_repository_refuses_names_no_ledger_to_refresh(tmp_path, monkeypatch, capsys):
    folder = tmp_path / "ledgers"
    folder.mkdir()
    monkeypatch.setattr(server.core, "LEDGER_DIR", folder)
    (folder / server.CARRY).write_text('{"demo": [7, "{}"]}')

    def refuse(carried):
        raise ValueError("bad rows")

    monkeypatch.setattr(server.stored, "adopt_carried", refuse)
    assert server.take_carry() is None
    assert capsys.readouterr().err == "carry: bad rows\n"


def stream_view(slug):
    return {"ledger": {"_meta": {"rev": 1}, "slug": slug}}


def test_the_loaded_modules_are_named_in_order_without_the_main_script():
    modules = {
        "zlib": object(),
        "__main__": object(),
        "json": object(),
        "gone": None,
        "ledger_server": object(),
        "scripts.swarm_ledger.ledger_server": object(),
        "old_ledger_server": object(),
    }
    assert server.loaded_modules(modules) == "json,old_ledger_server,zlib"
    assert server.MODULES == "LEDGER_MODULES"
    assert "json" in server.loaded_modules().split(",")


def test_a_successor_loads_the_modules_its_predecessor_held_and_skips_missing_ones():
    loaded = []

    def load(name):
        loaded.append(name)
        if name == "gone":
            raise ModuleNotFoundError(name)

    environ = {server.MODULES: "json,gone,ledger_core", "KEEP": "1"}
    server.warm_modules(environ, load)
    assert loaded == ["json", "gone", "ledger_core"]
    assert environ == {"KEEP": "1"}
    server.warm_modules({server.MODULES: ""}, load)
    server.warm_modules({}, load)
    assert loaded == ["json", "gone", "ledger_core"]


def test_warming_modules_imports_them(monkeypatch):
    monkeypatch.delitem(sys.modules, "colorsys", raising=False)
    server.warm_modules({server.MODULES: "colorsys"})
    assert "colorsys" in sys.modules


def test_warming_streams_builds_each_watched_view_and_leaves_it_unwatched(monkeypatch):
    hub = server.Hub()
    monkeypatch.setattr(server, "HUB", hub)
    monkeypatch.setattr(server, "stream_resources", stream_view)
    environ = {server.WATCHED: "demo,other", "KEEP": "1"}
    assert server.warm_streams(environ) == ["demo", "other"]
    assert environ == {"KEEP": "1"}
    assert hub.resource("demo", "ledger") == {"_meta": {"rev": 1}, "slug": "demo"}
    assert hub.resource("other", "ledger") == {"_meta": {"rev": 1}, "slug": "other"}
    assert hub.watched() == []
    assert server.WATCHED == "LEDGER_WATCHED"


def test_warming_streams_without_watched_ledgers_builds_nothing(monkeypatch):
    hub = server.Hub()
    monkeypatch.setattr(server, "HUB", hub)
    monkeypatch.setattr(server, "stream_resources", lambda slug: pytest.fail(f"built {slug}"))
    server.warm_streams({server.WATCHED: ""})
    server.warm_streams({})
    assert hub.channels == {}


def test_a_stream_view_that_fails_to_build_is_left_for_the_page(monkeypatch, capsys):
    hub = server.Hub()
    monkeypatch.setattr(server, "HUB", hub)

    def resources(slug):
        if slug == "broken":
            raise ValueError("bad rows")
        return stream_view(slug)

    monkeypatch.setattr(server, "stream_resources", resources)
    assert server.warm_streams({server.WATCHED: "broken,demo"}) == ["demo"]
    assert hub.resource("broken", "ledger") is None
    assert hub.resource("demo", "ledger") == {"_meta": {"rev": 1}, "slug": "demo"}
    assert hub.watched() == []
    assert capsys.readouterr().err == "warm stream broken: bad rows\n"


class Watching:
    def __init__(self, *slugs):
        self.slugs = slugs

    def has(self, slug):
        return slug in self.slugs


def test_refreshing_streams_publishes_only_the_carried_ledgers_a_page_watches(monkeypatch):
    published = []
    monkeypatch.setattr(server, "HUB", Watching("demo"))
    monkeypatch.setattr(server.repository, "get_document", lambda slug: {"slug": slug})
    monkeypatch.setattr(server, "publish_ledger", lambda slug, state: published.append((slug, state)))
    server.refresh_streams(["demo", "quiet"])
    assert published == [("demo", {"slug": "demo"})]


def test_a_refresh_that_fails_leaves_the_others_refreshing(monkeypatch, capsys):
    published = []

    def document(slug):
        if slug == "broken":
            raise ValueError("bad rows")
        return {"slug": slug}

    monkeypatch.setattr(server, "HUB", Watching("broken", "demo"))
    monkeypatch.setattr(server.repository, "get_document", document)
    monkeypatch.setattr(server, "publish_ledger", lambda slug, state: published.append((slug, state)))
    server.refresh_streams(["broken", "demo"])
    assert published == [("demo", {"slug": "demo"})]
    assert capsys.readouterr().err == "refresh stream broken: bad rows\n"
