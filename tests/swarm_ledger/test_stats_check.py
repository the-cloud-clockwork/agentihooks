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
const chat = [
  {id: "m0", by: "boss", at: 90, text: "before"},
  {id: "m1", by: "eng", at: 120, text: "not the master"},
  {id: "m2", by: "boss", at: 130, text: "", deleted: true},
  {id: "m3", by: "boss", at: 150, text: "stats checked"},
  {id: "m4", by: "boss", at: 170, text: "later"},
];
"""


def test_no_check_sent_shows_nothing():
    run("assert.equal(statsCheck([], [], null), null);")


def test_a_click_shows_sent_before_the_server_records_it():
    run(EVENTS + 'assert.deepEqual(statsCheck([], [], {id: "s9", at: 50}), {state: "sent", at: 50});')
    run(EVENTS + 'assert.deepEqual(statsCheck([sent, answer], chat, {id: "s9", at: 200}), {state: "sent", at: 200});')


def test_a_recorded_check_stays_sent_until_the_master_acks_it():
    run(EVENTS + 'assert.deepEqual(statsCheck([sent], chat, {id: "s1", at: 95}), {state: "sent", at: 100});')
    run(EVENTS + 'assert.deepEqual(statsCheck([sent, {...answer, id: "old"}], chat, null), {state: "sent", at: 100});')


def test_the_masters_ack_answers_with_its_time_and_first_reply_after_the_check():
    run(
        EVENTS
        + 'assert.deepEqual(statsCheck([sent, answer], chat, {id: "s1", at: 95}), {state: "answered", at: 100, answered_at: 160, by: "boss", reply: "m3"});'
    )
    run(
        EVENTS
        + 'assert.deepEqual(statsCheck([sent, answer], chat.slice(0, 3), null), {state: "answered", at: 100, answered_at: 160, by: "boss", reply: ""});'
    )


def test_only_the_latest_check_counts():
    run(
        EVENTS
        + 'const next = {...sent, id: "s2", at: 300};'
        + 'assert.deepEqual(statsCheck([sent, answer, next], chat, null), {state: "sent", at: 300});'
    )


def test_the_button_and_the_stats_card_render_the_check():
    page = TEMPLATE.read_text(encoding="utf-8")
    assert '<span class="sync-state end" id="stats-state"></span>' in page
    render = function_source("renderSync")
    assert "statsCheck(meta.events || [], doc.chat, statsSent)" in render
    stats = function_source("renderStats")
    assert 'row("Stats check"' in stats
    assert "showReply(ev, check.reply)" in stats
    chat = function_source("renderChat")
    assert "id: `chat-${e.id}`" in chat
