"""Live ledger changes on the page: a change rebuilds only the rows it touches and every row still shows its inputs."""

import json
import time
from typing import Any

import pytest

from tests.swarm_ledger import live_burst
from tests.swarm_ledger.ledger_page import ledger_state, served_ledger, shell_html, show
from tests.swarm_ledger.test_caps_columns import browser as chromium_browser

browser = chromium_browser

INSTALL = """async () => {
  const main = document.querySelector('script[type=module]').src;
  const sync = await import(new URL('sync.js', main));
  const settle = async () => {
    await new Promise((done) => setTimeout(done, 400));
    await new Promise((done) => {
      let timer = 0;
      const watch = new MutationObserver(() => { clearTimeout(timer); timer = setTimeout(stop, 250); });
      const stop = () => { watch.disconnect(); done(); };
      watch.observe(document.body, {subtree: true, childList: true, attributes: true, characterData: true});
      timer = setTimeout(stop, 250);
    });
  };
  const rows = () => new Map([...document.querySelectorAll('li[id^="item-"]')].map((el) => [el.id, el]));
  window.liveLedger = {
    state: null,
    async load(first, open) {
      for (const id of open) document.getElementById(id).open = true;
      this.state = first;
      sync.receiveLedger(first);
      await settle();
    },
    async apply(step) {
      const before = rows();
      const s = this.state;
      const next = {...s, _meta: {...s._meta, ...(step.meta || {}), rev: s._meta.rev + 1}};
      if (step.marks) next.marks = step.marks;
      if (step.list) {
        const items = s[step.list];
        next[step.list] = step.add ? [...items, step.add] : items.map((i) => (i.id === step.id ? {...i, ...step.set} : i));
      }
      this.state = next;
      sync.receiveLedger(next);
      await settle();
      return [...rows()].filter(([id, el]) => before.get(id) !== el).map(([id]) => id).sort();
    },
  };
}"""


def task(n: int, **fields: Any) -> dict:
    return {"id": f"t{n}", "title": f"Task {n}", "state": "open", "lane": "eng", "rank": "normal", **fields}


def opened(browser: Any, doc: dict, open_: tuple = ("tasks-box",)) -> Any:
    page = browser.new_page(viewport={"width": 1920, "height": 1080})
    show(page, shell_html(), ledger=ledger_state(doc))
    page.evaluate(INSTALL)
    first = served_ledger(ledger_state(doc, rev=2))
    page.evaluate("([first, open]) => window.liveLedger.load(first, open)", [first, list(open_)])
    return page


def apply(page: Any, step: dict) -> list:
    return page.evaluate("(step) => window.liveLedger.apply(step)", step)


def texts(page: Any, selector: str) -> list:
    return page.eval_on_selector_all(selector, "(els) => els.map((el) => el.textContent)")


def ids(page: Any, selector: str) -> list:
    return page.eval_on_selector_all(selector, "(els) => els.map((el) => el.id)")


def blocker(page: Any, n: int) -> str:
    return page.evaluate(f"() => document.querySelector('#item-tasks-t{n} p.desc')?.textContent || ''")


def stat(page: Any, label: str) -> str:
    return page.evaluate(
        "(label) => [...document.querySelectorAll('#stats .stat')]"
        ".find((row) => row.querySelector('dt').textContent.includes(label))?.querySelector('dd').textContent",
        label,
    )


@pytest.mark.feature("ledger-live-row-updates")
def test_a_live_change_to_one_task_rebuilds_only_its_row_and_updates_its_counts(browser: Any) -> None:
    page = opened(browser, {"title": "Live rows", "tasks": [task(n) for n in range(40)]})
    assert apply(page, {"list": "tasks", "id": "t7", "set": {"state": "claimed", "claimed_by": "engineer@1"}}) == [
        "item-tasks-t7"
    ]
    assert page.text_content("#tasks-count") == "· 39 open · 1 claimed"
    assert ids(page, "#tasks > li[id]")[:2] == ["item-tasks-t7", "item-tasks-t0"]
    assert "claimed by engineer@1" in page.text_content("#item-tasks-t7")
    page.close()


@pytest.mark.feature("ledger-live-row-updates")
def test_a_live_change_to_one_phase_rebuilds_only_its_row(browser: Any) -> None:
    phases = [{"id": f"p{n}", "title": f"Phase {n}", "plan": "plans/pl0", "description": "Before"} for n in range(10)]
    doc = {"title": "Live phases", "plans": [{"id": "pl0", "title": "Plan zero"}], "phases": phases}
    page = opened(browser, doc, ("phases-box",))
    assert apply(page, {"list": "phases", "id": "p3", "set": {"description": "After"}}) == ["item-phases-p3"]
    assert page.text_content("#item-phases-p3 .desc") == "After"
    assert texts(page, "#item-plans-pl0 .plan-count") == ["10 phases"]
    page.close()


@pytest.mark.feature("ledger-task-blockers-text")
def test_waiting_ready_and_parked_tasks_name_their_blockers_until_the_blocker_finishes(browser: Any) -> None:
    tasks = [
        task(0, title="Task zero"),
        task(1, depends_on=["t0"]),
        task(2, title="Task two", state="claimed", branch="b2"),
        task(3, depends_on=["t2"]),
        task(4, parked_on=["t0"], branch="w4"),
        task(5, depends_on=["gone"]),
    ]
    closed = {"tasks": {"hidden": 1, "kept": 10, "ids": ["gone"]}}
    page = opened(browser, {"title": "Blockers", "tasks": tasks, "_meta": {"closed": closed}})
    assert blocker(page, 1) == "Waiting until Task zero is done"
    assert blocker(page, 3) == "Ready to start on branch b2 of Task two"
    assert blocker(page, 4) == "Parked on branch w4 until Task zero is done"
    assert blocker(page, 5) == ""
    apply(page, {"list": "tasks", "id": "t0", "set": {"state": "done"}})
    assert (blocker(page, 1), blocker(page, 4)) == ("", "")
    assert blocker(page, 3) == "Ready to start on branch b2 of Task two"
    page.close()


@pytest.mark.feature("ledger-live-row-updates")
def test_rank_and_state_changes_move_a_live_row_to_its_place(browser: Any) -> None:
    page = opened(browser, {"title": "Moves", "tasks": [task(n) for n in range(6)]})
    apply(page, {"list": "tasks", "id": "t4", "set": {"rank": "urgent"}})
    assert ids(page, "#tasks > li[id^='item-']") == [f"item-tasks-t{n}" for n in (4, 0, 1, 2, 3, 5)]
    apply(page, {"list": "tasks", "id": "t2", "set": {"state": "done"}})
    assert ids(page, "#tasks > li[id^='item-']") == [f"item-tasks-t{n}" for n in (4, 0, 1, 3, 5)]
    assert ids(page, "#tasks-done li[id^='item-']") == ["item-tasks-t2"]
    assert page.text_content("#tasks-done .done-head") == "1 done"
    page.close()


@pytest.mark.feature("ledger-live-row-updates")
def test_a_live_change_keeps_a_comment_draft_and_its_focus(browser: Any) -> None:
    page = opened(browser, {"title": "Draft", "tasks": [task(n) for n in range(10)]})
    page.click("#item-tasks-t3 details[data-key] > summary")
    page.click("#item-tasks-t3 .thread button.add")
    page.fill("#item-tasks-t3 textarea", "draft words")
    note = {"id": "c1", "by": "engineer@1", "at": int(time.time() * 1000), "text": "progress"}
    apply(page, {"list": "tasks", "id": "t8", "set": {"comments": [note]}})
    assert page.input_value("#item-tasks-t3 textarea") == "draft words"
    assert page.evaluate("() => document.activeElement.closest('li')?.id") == "item-tasks-t3"
    page.close()


@pytest.mark.feature("ledger-live-row-updates")
def test_a_freeze_mark_reaches_a_row_whose_task_did_not_change(browser: Any) -> None:
    page = opened(browser, {"title": "Marks", "tasks": [task(n) for n in range(4)]})
    apply(page, {"marks": {"frozen": ["tasks/t2"], "held": []}})
    assert "frozen" in page.get_attribute("#item-tasks-t2", "class").split()
    assert page.locator("#item-tasks-t2 > .snowflake").count() == 1
    page.close()


@pytest.mark.feature("ledger-live-row-updates")
def test_a_new_task_in_a_slice_updates_its_phase_slice_count(browser: Any) -> None:
    doc = {
        "title": "Slices",
        "plans": [{"id": "pl0", "title": "Plan zero"}],
        "phases": [{"id": "p0", "title": "Phase zero", "plan": "plans/pl0"}],
        "slices": [{"id": "s0", "anchor": "s-zero", "phase": "phases/p0"}],
        "tasks": [task(n, slice="slices/s0") for n in range(2)],
    }
    page = opened(browser, doc, ("phases-box",))
    assert texts(page, "#item-slices-s0 .slice-name") == ["s-zero 2"]
    apply(page, {"list": "tasks", "add": task(2, slice="slices/s0")})
    assert texts(page, "#item-slices-s0 .slice-name") == ["s-zero 3"]
    page.close()


@pytest.mark.feature("ledger-live-row-updates")
def test_the_agents_stat_counts_a_new_commenter_after_a_live_change(browser: Any) -> None:
    now = int(time.time() * 1000)
    first = task(0, comments=[{"id": "c0", "by": "agent-a", "at": now, "text": "start"}])
    page = opened(browser, {"title": "Agents", "tasks": [first, task(1)]})
    assert stat(page, "Agents") == "1"
    note = {"id": "c1", "by": "agent-b", "at": now, "text": "joined"}
    apply(page, {"list": "tasks", "id": "t1", "set": {"comments": [note]}})
    assert stat(page, "Agents") == "2"
    page.close()


@pytest.mark.feature("ledger-live-row-updates")
def test_the_sync_badge_counts_agents_that_have_not_synced(browser: Any) -> None:
    now = int(time.time() * 1000)
    meta = {
        "events": [{"kind": "sync requested", "rev": 5, "at": now}],
        "crew": [{"name": "a", "handled_rev": 3}, {"name": "b", "handled_rev": 6}],
    }
    page = opened(browser, {"title": "Sync", "tasks": [task(0)], "_meta": meta})
    assert (page.is_visible("#sync-badge"), page.text_content("#sync-badge")) == (True, "1")
    apply(page, {"meta": {"crew": [{"name": "a", "handled_rev": 6}, {"name": "b", "handled_rev": 6}]}})
    assert page.is_hidden("#sync-badge")
    page.close()


@pytest.mark.feature("ledger-cache-first-screen")
def test_live_changes_defer_the_cache_write_and_keep_it_to_the_first_screen(browser: Any) -> None:
    page = browser.new_page(viewport={"width": 1920, "height": 1080})
    page.add_init_script(
        """(() => {
          const post = Worker.prototype.postMessage;
          window.cachePosts = 0;
          Worker.prototype.postMessage = function(data, ...rest) {
            if (data?.value?.doc) window.cachePosts++;
            return post.call(this, data, ...rest);
          };
        })();"""
    )
    tasks = [task(n, rank="urgent" if n >= 100 else "normal") for n in range(120)]
    doc = {"title": "Cache", "tasks": tasks}
    show(page, shell_html(), ledger=ledger_state(doc))
    page.evaluate(INSTALL)
    page.evaluate("(first) => window.liveLedger.load(first, [])", served_ledger(ledger_state(doc, rev=2)))
    page.evaluate("() => { window.cachePosts = 0; }")
    for n in range(8):
        note = {"id": f"c{n}", "by": "engineer@1", "at": int(time.time() * 1000), "text": f"change {n}"}
        page.evaluate(
            "(step) => { window.liveLedger.apply(step); }",
            {"list": "tasks", "id": f"t{n}", "set": {"comments": [note]}},
        )
        page.wait_for_timeout(50)
    page.wait_for_function("() => window.cachePosts > 0", timeout=15000)
    page.wait_for_timeout(2000)
    cached = page.evaluate(
        """() => Object.values(localStorage).map((value) => { try { return JSON.parse(value); } catch { return null; } })
          .find((value) => value?.doc?.title === 'Cache')?.doc.tasks.map((t) => t.id)"""
    )
    assert page.evaluate("window.cachePosts") == 1
    assert cached[:20] == [f"t{n}" for n in range(100, 120)]
    assert len(cached) == 50
    page.close()


@pytest.mark.feature("ledger-live-row-updates")
def test_a_burst_of_live_changes_on_a_production_size_ledger_rebuilds_only_the_changed_rows(browser: Any) -> None:
    state = live_burst.seeded(2)
    page, _ = live_burst.opened(browser, 1)
    reading = live_burst.replay(page, state, live_burst.changes(state, 6, 3), 400)
    print(json.dumps(reading))
    assert reading["rows_rebuilt_median"] <= 1
    assert reading["rows_rebuilt_max"] <= 5
    assert {"long_tasks", "longest_task_ms", "render_ms_median", "open_section_ms"} <= reading.keys()
    page.close()
