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


def test_outline_marking_does_not_read_geometry_after_writing_link_classes(browser):
    page = browser.new_page(viewport={"width": 1920, "height": 1080})
    show(page, shell_html(), ledger=ledger_state({"title": "Outline"}))
    result = page.evaluate(
        """async () => {
          const main = document.querySelector('script[type=module]').src;
          const {markOutline} = await import(new URL('outline.js', main));
          const nav = document.getElementById('outline');
          const link = nav.querySelector('a');
          link.classList.remove('on');
          document.getElementById(link.dataset.target).getBoundingClientRect = () => ({top: 0});
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
    assert result["reads"]
    assert not any(result["reads"])
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
