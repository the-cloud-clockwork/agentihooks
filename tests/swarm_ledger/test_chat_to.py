import json
import re
import subprocess

import new_ledger
import pytest

from tests.swarm_ledger.test_design_system import SCRIPTS, palette
from tests.swarm_ledger.test_swarm_layout import browser, open_page, status
from tests.swarm_ledger.test_swarm_panel import function_source

__all__ = ["browser", "open_page"]


@pytest.fixture(scope="module")
def page():
    content = {"title": "To", "overview": "o", "sources": [], "phases": [], "questions": [], "followups": []}
    return new_ledger.render(new_ledger.build_doc(content), "chatto", 8765)


@pytest.fixture
def page_script():
    functions = "\n".join(
        function_source(n) for n in ("renderChatTo", "setChatTo", "chatText", "chatWho", "withAttachments", "sendChat")
    )
    return (
        "const SWARM = 'swarm'; const nodes = {}; let sent = []; const attaching = {}; const attachNote = {};"
        "const refreshTray = () => {};"
        "const $ = id => nodes[id] ||= {value: '', hidden: true, kids: [], children: [], textContent: '',"
        "replaceChildren(...k) { this.kids = k; this.children = k; }};"
        "const h = (tag, attrs) => ({tag, ...attrs, dataset: {to: attrs['data-to']},"
        "setAttribute(k, v) { this[k] = v; }});"
        "const grow = () => {}; const newId = () => 'm-1'; const queue = op => sent.push(op.text);"
        + functions
        + "\nconst sw = {agents: [{name: 'sw-master-1', lane: 'master', state: 'working'},"
        "{name: 'sw-eng-1', lane: 'eng', state: 'working'}, {name: 'sw-ci-1', lane: 'ci', state: 'finished'}]};"
    )


def run_page(script, expression):
    result = subprocess.run(["node", "-e", script + expression], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def test_the_composer_states_its_recipient_master_by_default(page):
    compose = page.split('<div class="chat-compose">', 1)[1]
    assert re.search(r'<span id="chat-to-label">To</span><input type="hidden" id="chat-to" value="">', compose)
    assert re.search(r'<button type="button" id="chat-to-pick" aria-haspopup="listbox"[^>]*>master</button>', compose)
    assert re.search(r'<div class="chat-to-menu" id="chat-to-menu" role="listbox"[^>]*hidden>', compose)


def test_the_picker_offers_master_the_whole_swarm_and_each_live_agent(page_script):
    result = run_page(
        page_script,
        "renderChatTo(sw); console.log(JSON.stringify($('chat-to-menu').kids.map(o => [o['data-to'], o.text, o.role,"
        "o['aria-selected']])));",
    )
    assert result == [
        ["", "master", "option", "true"],
        ["swarm", "swarm", "option", "false"],
        ["sw-eng-1", "sw-eng-1", "option", "false"],
    ]


def test_an_unaddressed_line_goes_plain_and_a_picked_agent_gets_an_at_line(page_script):
    result = run_page(
        page_script,
        "renderChatTo(sw); sendChat('hello'); setChatTo('sw-eng-1'); sendChat('look here');"
        "sendChat('@sw-ci-1 typed by hand'); console.log(JSON.stringify([sent, $('chat-to-pick').textContent]));",
    )
    assert result == [["hello", "@sw-eng-1 look here", "@sw-ci-1 typed by hand"], "sw-eng-1"]


def test_a_line_to_the_swarm_is_one_at_swarm_line_marked_for_the_whole_swarm(page_script):
    result = run_page(
        page_script,
        "renderChatTo(sw); setChatTo('swarm'); sendChat('all stop');"
        "console.log(JSON.stringify([sent, chatWho({by: 'operator', text: sent[0]}),"
        "chatWho({by: 'operator', text: 'hello'}), chatWho({by: 'sw-eng-1', text: '@swarm hi'})]));",
    )
    assert result == [["@swarm all stop"], "You to the whole swarm", "You", "sw-eng-1"]


def test_the_picked_swarm_survives_a_rerender(page_script):
    result = run_page(
        page_script,
        "renderChatTo(sw); setChatTo('swarm'); renderChatTo(sw);"
        "console.log(JSON.stringify([$('chat-to').value, $('chat-to-pick').textContent]));",
    )
    assert result == ["swarm", "swarm"]


def test_a_picked_agent_that_finished_falls_back_to_master(page_script):
    result = run_page(
        page_script,
        "renderChatTo(sw); setChatTo('sw-eng-1'); sw.agents[1].state = 'finished'; renderChatTo(sw);"
        "console.log(JSON.stringify([$('chat-to').value, $('chat-to-pick').textContent]));",
    )
    assert result == ["", "master"]


def test_an_open_list_is_not_rebuilt_under_the_pointer(page_script):
    result = run_page(
        page_script,
        "renderChatTo(sw); $('chat-to-menu').hidden = false; sw.agents[1].state = 'finished'; renderChatTo(sw);"
        "console.log(JSON.stringify($('chat-to-menu').kids.length));",
    )
    assert result == 3


def test_the_list_sits_on_an_opaque_palette_token_with_bare_labels():
    css = (SCRIPTS / "template.html").read_text(encoding="utf-8")
    tokens = dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", palette()))
    menu = re.search(r"\.chat-to-menu \{([^}]*)\}", css).group(1)
    background = re.search(r"background: var\((--[\w-]+)\)", menu).group(1)
    assert re.fullmatch(r"#[0-9a-f]{6}", tokens[tokens[background][len("var(") : -1]])
    assert "border" not in menu and "scrollbar-width: none" in menu
    label = re.search(r"\.chat-to button \{([^}]*)\}", css).group(1)
    assert "background: transparent" in label and "border: 0" in label and "color: var(--accent)" in label


def compose(page):
    page.tab.locator("#chat-fab").click()
    return page.tab.locator("#chat-to-pick"), page.tab.locator("#chat-to-menu")


def test_the_picker_opens_on_click_above_the_message_box_and_closes_on_a_pick(open_page):
    page = open_page(status())
    pick, menu = compose(page)
    assert menu.is_hidden() and pick.inner_text() == "master"
    pick.click()
    assert menu.is_visible() and pick.get_attribute("aria-expanded") == "true"
    assert menu.locator("[role=option]").all_inner_texts() == ["master", "swarm", "eng-58"]
    row = menu.get_by_role("option", name="swarm")
    style = "el => [getComputedStyle(el).textShadow, getComputedStyle(el).backgroundColor, getComputedStyle(el).borderTopWidth]"
    glow, rest, edge = row.evaluate(style)
    assert glow != "none" and rest == "rgba(0, 0, 0, 0)" and edge == "0px"
    row.hover()
    assert row.evaluate(style)[1] != rest
    above = menu.bounding_box()["y"] + menu.bounding_box()["height"]
    assert above <= page.tab.locator("#chat-input").bounding_box()["y"]
    menu.get_by_role("option", name="swarm").click()
    assert menu.is_hidden() and pick.inner_text() == "swarm"
    assert page.tab.locator("#chat-to").input_value() == "swarm"


def test_the_list_answers_arrows_and_escape_without_closing_the_chat(open_page):
    page = open_page(status())
    pick, menu = compose(page)
    pick.click()
    page.tab.keyboard.press("ArrowDown")
    page.tab.keyboard.press("Enter")
    assert pick.inner_text() == "swarm"
    pick.click()
    page.tab.keyboard.press("Escape")
    assert menu.is_hidden() and page.tab.locator("#chat-panel").is_visible()


def test_a_click_outside_closes_the_list(open_page):
    page = open_page(status())
    pick, menu = compose(page)
    pick.click()
    page.tab.locator("#chat-log").click()
    assert menu.is_hidden() and pick.get_attribute("aria-expanded") == "false"
