import subprocess
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger" / "template.html"


def function_source(name):
    page = TEMPLATE.read_text(encoding="utf-8")
    return f"function {name}(" + page.split(f"  function {name}(", 1)[1].split("\n  }\n", 1)[0] + "\n}"


def run(body):
    script = function_source("statsCheck") + '\nconst assert = require("node:assert/strict");\n' + body
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


EVENTS = """
const sent = {kind: "stats sync requested", id: "s1", at: 100, by: "operator"};
const answer = {kind: "stats check answered", id: "s1", at: 160, by: "boss"};
"""


def test_no_check_sent_shows_nothing():
    run("assert.equal(statsCheck([], null), null);")


def test_a_click_shows_sent_before_the_server_records_it():
    run(EVENTS + 'assert.deepEqual(statsCheck([], {id: "s9", at: 50}), {state: "sent", at: 50});')
    run(EVENTS + 'assert.deepEqual(statsCheck([sent, answer], {id: "s9", at: 200}), {state: "sent", at: 200});')


def test_a_recorded_check_stays_sent_until_the_master_acks_it():
    run(EVENTS + 'assert.deepEqual(statsCheck([sent], {id: "s1", at: 95}), {state: "sent", at: 100});')
    run(EVENTS + 'assert.deepEqual(statsCheck([sent, {...answer, id: "old"}], null), {state: "sent", at: 100});')


def test_the_masters_ack_answers_with_its_time():
    run(
        EVENTS
        + 'assert.deepEqual(statsCheck([sent, answer], {id: "s1", at: 95}), {state: "answered", at: 100, answered_at: 160});'
    )


def test_only_the_latest_check_counts():
    run(
        EVENTS
        + 'const next = {...sent, id: "s2", at: 300};'
        + 'assert.deepEqual(statsCheck([sent, answer, next], null), {state: "sent", at: 300});'
    )


def test_the_header_shows_the_check_beside_the_refresh_button():
    page = TEMPLATE.read_text(encoding="utf-8")
    assert '<span class="sync-state end" id="stats-state"></span><button class="mini-sync" id="stats-sync"' in page
    render = function_source("renderSync")
    assert "statsCheck(meta.events || [], statsSent)" in render
    assert 'const checked = check ? `Stats check ${statsCheckText(check)}` : "";' in render
    assert '$("stats-sync").ariaLabel = [checked, ' in render
    assert '$("stats-state").textContent = check ? statsCheckText(check) : "";' in render


def test_the_stats_card_has_no_stats_check_row():
    script = (
        "const nodes = {}; const $ = id => nodes[id] ||= {replaceChildren: (...rows) => {nodes[id].textContent = rows.join(' · ');}};\n"
        "const h = (tag, attrs, ...children) => attrs.text || children.filter(Boolean).join(' ');\n"
        "const when = () => 'now'; const span = () => '1m'; const inScope = list => list; const activeAgents = () => 0;\n"
        + EVENTS
        + "let meta = {created_at: 1, events: [sent, answer]};\n"
        "let doc = {phases: [], followups: [], questions: [], tasks: [], time_left_minutes: null, chat: [{id: 'm3', by: 'boss', at: 150, text: 'ok'}]};\n"
        "const statsSent = null;\n"
        + function_source("statsCheck")
        + function_source("statsCheckText")
        + function_source("renderStats")
        + "\nconst assert = require('node:assert/strict');\n"
        "renderStats();\n"
        "assert.equal(statsCheckText(statsCheck(meta.events, statsSent)), 'answered now');\n"
        "assert.doesNotMatch(nodes.stats.textContent, /Stats check|answered|reply/);\n"
    )
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    assert "showReply" not in TEMPLATE.read_text(encoding="utf-8")
