import pytest

from tests.swarm_ledger.ledger_page import ledger_state, shell_html, show
from tests.swarm_ledger.test_caps_columns import browser as chromium_browser

browser = chromium_browser


def test_first_snapshot_does_not_serialize_a_full_view_to_compare_it_with_null(browser):
    page = browser.new_page(viewport={"width": 1920, "height": 1080})
    page.add_init_script(
        """(() => {
          const stringify = JSON.stringify;
          window.snapshotComparisons = 0;
          JSON.stringify = function(value, ...args) {
            if (value?.snapshotMarker) window.snapshotComparisons++;
            return stringify.call(this, value, ...args);
          };
        })();"""
    )
    show(page, shell_html(), ledger=ledger_state({"title": "Snapshot", "snapshotMarker": True}))
    assert page.locator("#title").text_content() == "Snapshot"
    assert page.evaluate("window.snapshotComparisons") == 0
    with page.expect_request("**/events"):
        page.wait_for_timeout(1500)
    assert page.evaluate("window.snapshotComparisons") == 0
    page.close()


def test_snapshot_parsing_yields_before_applying_the_view(browser):
    page = browser.new_page(viewport={"width": 1920, "height": 1080})
    page.add_init_script(
        """(() => {
          const parse = JSON.parse;
          JSON.parse = function(...args) {
            const value = parse.apply(this, args);
            if (value?.ledger?.snapshotMarker) {
              value.ledger.title = 'Parsed snapshot';
              setTimeout(() => { window.titleAfterParsing = document.getElementById('title').textContent; }, 0);
            }
            return value;
          };
        })();"""
    )
    show(page, shell_html(), ledger=ledger_state({"title": "Before snapshot", "snapshotMarker": True}))
    page.wait_for_function("window.titleAfterParsing !== undefined")
    assert page.evaluate("window.titleAfterParsing") != "Parsed snapshot"
    page.wait_for_function("document.getElementById('title').textContent === 'Parsed snapshot'")
    page.close()


def test_outline_marking_does_not_read_geometry_after_writing_link_classes(browser):
    page = browser.new_page(viewport={"width": 1920, "height": 1080})
    show(page, shell_html(), ledger=ledger_state({"title": "Outline"}))
    result = page.evaluate(
        """async () => {
          const main = document.querySelector('script[type=module]').src;
          const {markOutline} = await import(new URL('outline.js', main));
          const nav = document.getElementById('outline');
          const link = nav.querySelector('a');
          link.click();
          const reads = [];
          let wrote = false;
          for (const a of nav.querySelectorAll('a')) {
            const toggle = a.classList.toggle.bind(a.classList);
            a.classList.toggle = (...args) => { wrote = true; return toggle(...args); };
            Object.defineProperty(a, 'offsetTop', {get() { reads.push(wrote); return 0; }});
          }
          markOutline();
          return {reads, active: nav.querySelectorAll('a.on').length};
        }"""
    )
    assert result["active"] == 1
    assert not any(result["reads"])
    page.close()


@pytest.mark.parametrize(("height", "end_top"), [(400, 88), (20, -100)])
def test_outline_updates_when_a_target_crosses_or_jumps_over_the_activation_line(browser, height, end_top):
    page = browser.new_page(viewport={"width": 1920, "height": 1080})
    show(page, shell_html(), ledger=ledger_state({"title": "Outline crossing"}))
    page.evaluate(
        """async (height) => {
          const main = document.querySelector(\'script[type=module]\').src;
          const {markOutline} = await import(new URL(\'outline.js\', main));
          const target = document.createElement(\'section\');
          target.id = \'outline-crossing\';
          target.style.cssText = `position:fixed;top:300px;left:400px;width:100px;height:${height}px`;
          document.body.append(target);
          const row = document.createElement(\'li\');
          row.className = \'ol-sec\';
          row.innerHTML = \'<a href="#outline-crossing" data-target="outline-crossing">Crossing</a>\';
          document.querySelector(\'#outline > ul\').append(row);
          markOutline();
          await new Promise(requestAnimationFrame);
          await new Promise(requestAnimationFrame);
        }""",
        height,
    )
    page.evaluate(
        """top => {
          document.getElementById(\'outline-crossing\').style.top = `${top}px`;
          window.dispatchEvent(new Event(\'scroll\'));
        }""",
        end_top,
    )
    page.wait_for_function(
        "() => document.querySelector('a[data-target=outline-crossing]').classList.contains('on')",
        timeout=3000,
    )
    page.evaluate(
        """() => {
          document.getElementById(\'outline-crossing\').style.top = \'300px\';
          window.dispatchEvent(new Event(\'scroll\'));
        }"""
    )
    page.wait_for_function(
        "() => !document.querySelector('a[data-target=outline-crossing]').classList.contains('on')",
        timeout=3000,
    )
    page.close()


def test_a_changed_reconnect_snapshot_updates_without_serializing_the_full_view(browser):
    page = browser.new_page(viewport={"width": 1920, "height": 1080})
    page.add_init_script(
        """(() => {
          const stringify = JSON.stringify;
          window.snapshotComparisons = 0;
          JSON.stringify = function(value, ...args) {
            if (value?.snapshotMarker) window.snapshotComparisons++;
            return stringify.call(this, value, ...args);
          };
        })();"""
    )
    ledger = ledger_state({"title": "Before reconnect", "snapshotMarker": True})
    show(page, shell_html(), ledger=ledger)
    ledger["title"] = "After reconnect"
    page.wait_for_function("() => document.getElementById('title').textContent === 'After reconnect'", timeout=5000)
    assert page.evaluate("window.snapshotComparisons") == 0
    page.close()


def test_stream_rendering_yields_to_input_between_sections(browser):
    page = browser.new_page(viewport={"width": 1920, "height": 1080})
    show(page, shell_html(), ledger=ledger_state({"title": "Yielding"}))
    result = page.evaluate(
        """async () => {
          const main = document.querySelector('script[type=module]').src;
          const {render} = await import(new URL('render.js', main));
          let inputHandled = false;
          setTimeout(() => { inputHandled = true; }, 0);
          await render(null, true);
          return {inputHandled, title: document.getElementById('title').textContent};
        }"""
    )
    assert result == {"inputHandled": True, "title": "Yielding"}
    page.close()


def test_browser_cache_serializes_the_document_off_the_main_thread_and_keeps_pending_edits(browser):
    page = browser.new_page(viewport={"width": 1920, "height": 1080})
    page.add_init_script(
        """(() => {
          const stringify = JSON.stringify;
          window.cacheSerializations = 0;
          JSON.stringify = function(value, ...args) {
            if (value?.doc?.title === 'Cache proof') window.cacheSerializations++;
            return stringify.call(this, value, ...args);
          };
        })();"""
    )
    show(page, shell_html(), ledger=ledger_state({"title": "Cache proof"}))
    result = page.evaluate(
        """async () => {
          const main = document.querySelector('script[type=module]').src;
          const sync = await import(new URL('sync.js', main));
          const state = await import(new URL('state.js', main));
          sync.ops.push({op: 'add', thread: 'notes', id: 'pending', text: 'Keep this edit'});
          state.lsWrite();
          return {serializations: window.cacheSerializations, saved: state.lsRead().ops};
        }"""
    )
    assert result["serializations"] == 0
    assert result["saved"] == [{"op": "add", "thread": "notes", "id": "pending", "text": "Keep this edit"}]
    page.wait_for_function(
        """() => Object.values(localStorage).some(value => {
          try { return JSON.parse(value)?.doc?.title === 'Cache proof'; } catch { return false; }
        })"""
    )
    page.close()


def test_stream_render_does_not_steal_focus_after_the_user_moves_to_another_control(browser):
    page = browser.new_page(viewport={"width": 1920, "height": 1080})
    show(page, shell_html(), ledger=ledger_state({"title": "Focus"}))
    focused = page.evaluate(
        """async () => {
          const main = document.querySelector('script[type=module]').src;
          const {render} = await import(new URL('render.js', main));
          const field = document.createElement('textarea');
          field.dataset.focus = 'proof:focus';
          document.body.append(field);
          field.focus();
          setTimeout(() => document.getElementById('overview-edit').focus(), 0);
          await render(null, true);
          return document.activeElement.id;
        }"""
    )
    assert focused == "overview-edit"
    page.close()


def test_offscreen_phase_content_skips_layout_but_keeps_deep_navigation(browser):
    page = browser.new_page(viewport={"width": 1920, "height": 1080})
    phases = [{"id": f"p{i}", "title": f"Phase {i}", "description": "Details", "done": False} for i in range(30)]
    show(page, shell_html(), ledger=ledger_state({"title": "Phase layout", "phases": phases}))
    page.evaluate(
        """() => {
          document.getElementById(\'sec-phases\').style.marginTop = \'5000px\';
          document.getElementById(\'phases-box\').open = true;
        }"""
    )
    page.wait_for_function("() => !!document.getElementById('item-phases-p29')")
    page.evaluate("async () => { await new Promise(requestAnimationFrame); await new Promise(requestAnimationFrame); }")
    assert page.locator("#phases > li").count() == 30
    page.wait_for_function(
        "() => !document.querySelector('#item-phases-p29 .item-title').checkVisibility({contentVisibilityAuto: true})",
        timeout=3000,
    )
    page.evaluate(
        """async () => {
          const main = document.querySelector(\'script[type=module]\').src;
          const {revealTarget} = await import(new URL(\'outline.js\', main));
          revealTarget(\'item-phases-p29\').scrollIntoView({block: \'center\'});
        }"""
    )
    row = page.locator("#item-phases-p29")
    row.wait_for(state="visible")
    assert 0 <= row.bounding_box()["y"] < 1080
    assert "Phase 29" in row.text_content()
    page.close()


def test_full_ledger_keeps_task_pagination_and_deep_navigation(browser):
    page = browser.new_page(viewport={"width": 1920, "height": 1080})
    tasks = [{"id": f"t{i}", "title": f"Task {i}", "state": "open", "lane": "eng"} for i in range(2400)]
    show(page, shell_html(), ledger=ledger_state({"title": "Full ledger", "tasks": tasks}))
    page.locator("#tasks-box > summary").click()
    page.locator("#tasks > li[id]").nth(49).wait_for()
    assert page.locator("#tasks > li[id]").count() == 50
    page.locator("#tasks > .page-more button").click()
    page.locator("#tasks > li[id]").nth(99).wait_for()
    assert page.locator("#tasks > li[id]").count() == 100
    found = page.evaluate(
        """async () => {
          const main = document.querySelector('script[type=module]').src;
          const {revealTarget} = await import(new URL('outline.js', main));
          return revealTarget('item-tasks-t2399')?.textContent;
        }"""
    )
    assert "Task 2399" in found
    page.close()
