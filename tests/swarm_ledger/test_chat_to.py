import json
import re
import subprocess

import new_ledger
import pytest

from tests.swarm_ledger.test_swarm_panel import function_source


@pytest.fixture(scope="module")
def page():
    content = {"title": "To", "overview": "o", "sources": [], "phases": [], "questions": [], "followups": []}
    return new_ledger.render(new_ledger.build_doc(content), "chatto", 8765)


@pytest.fixture
def page_script():
    functions = "\n".join(function_source(n) for n in ("renderChatTo", "chatText", "withAttachments", "sendChat"))
    return (
        "const nodes = {}; let sent = []; const attaching = {}; const attachNote = {}; const refreshTray = () => {};"
        "const $ = id => nodes[id] ||= {value: '', kids: [], replaceChildren(...k) { this.kids = k; }};"
        "const h = (tag, attrs) => ({tag, ...attrs}); const document = {activeElement: null};"
        "const grow = () => {}; const newId = () => 'm-1'; const queue = op => sent.push(op.text);"
        + functions
        + "\nconst sw = {agents: [{name: 'sw-master-1', lane: 'master', state: 'working'},"
        "{name: 'sw-eng-1', lane: 'eng', state: 'working'}, {name: 'sw-ci-1', lane: 'ci', state: 'finished'}]};"
    )


def run_page(script, expression):
    result = subprocess.run(["node", "-e", script + expression], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def test_the_composer_states_its_recipient_master_by_default(page):
    compose = page.split('<div class="chat-compose">', 1)[1].split("</div></div>", 1)[0]
    assert re.search(r'<label[^>]*for="chat-to"[^>]*>To</label><select[^>]*id="chat-to"', compose)


def test_the_picker_offers_master_and_each_live_agent(page_script):
    result = run_page(
        page_script,
        "renderChatTo(sw); console.log(JSON.stringify($('chat-to').kids.map(o => [o.value, o.text])));",
    )
    assert result == [["", "master"], ["sw-eng-1", "sw-eng-1"]]


def test_an_unaddressed_line_goes_plain_and_a_picked_agent_gets_an_at_line(page_script):
    result = run_page(
        page_script,
        "renderChatTo(sw); sendChat('hello'); $('chat-to').value = 'sw-eng-1'; sendChat('look here');"
        "sendChat('@sw-ci-1 typed by hand'); console.log(JSON.stringify(sent));",
    )
    assert result == ["hello", "@sw-eng-1 look here", "@sw-ci-1 typed by hand"]


def test_a_picked_agent_that_finished_falls_back_to_master(page_script):
    result = run_page(
        page_script,
        "renderChatTo(sw); $('chat-to').value = 'sw-eng-1'; sw.agents[1].state = 'finished'; renderChatTo(sw);"
        "console.log(JSON.stringify($('chat-to').value));",
    )
    assert result == ""
