import subprocess
from pathlib import Path

from tests.swarm_ledger.ledger_page import page_source

TEMPLATE = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger" / "template.html"


def function_source(name):
    page = page_source()
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
    run(EVENTS + 'assert.deepEqual(statsCheck([], {id: "s9", at: 50}), {state: "pending", at: 50});')
    run(EVENTS + 'assert.deepEqual(statsCheck([sent, answer], {id: "s9", at: 200}), {state: "pending", at: 200});')


def test_a_recorded_check_stays_sent_until_the_master_acks_it():
    run(EVENTS + 'assert.deepEqual(statsCheck([sent], {id: "s1", at: 95}), {state: "pending", at: 100});')
    run(EVENTS + 'assert.deepEqual(statsCheck([sent, {...answer, id: "old"}], null), {state: "pending", at: 100});')


def test_master_ack_does_not_claim_deterministic_refresh_completed():
    run(EVENTS + 'assert.deepEqual(statsCheck([sent, answer], {id: "s1", at: 95}), {state: "pending", at: 100});')


def test_only_the_latest_check_counts():
    run(
        EVENTS
        + 'const next = {...sent, id: "s2", at: 300};'
        + 'assert.deepEqual(statsCheck([sent, answer, next], null), {state: "pending", at: 300});'
    )


def test_older_server_response_cannot_replace_newer_stats():
    script = (
        function_source("applyServer")
        + '\nconst assert = require("node:assert/strict");\n'
        + "let rev = 12, meta = {rev: 12}, doc = {time_left_minutes: 60};\n"
        + "let ops = [], seedBroken = false;\n"
        + "const withDefaults = x => x, applyChecks = () => {}, banner = () => {}, render = () => {}, lsWrite = () => {};\n"
        + "let firstState = () => {};\n"
        + "applyServer({_meta: {rev: 11}, time_left_minutes: 400});\n"
        + "assert.equal(rev, 12); assert.equal(doc.time_left_minutes, 60);\n"
        + "applyServer({_meta: {rev: 13}, time_left_minutes: 30});\n"
        + "assert.equal(rev, 13); assert.equal(doc.time_left_minutes, 30);\n"
    )
    import subprocess

    subprocess.run(["node", "-e", script], check=True)


def test_refresh_status_uses_persisted_completion_independently_of_master():
    run(
        EVENTS
        + 'const refresh = {id: "s1", state: "refreshed", at: 100, completed_at: 120};\n'
        + "assert.deepEqual(statsCheck([sent], null, refresh), refresh);\n"
        + "assert.deepEqual(statsCheck([sent, answer], null, refresh), refresh);\n"
        + 'const failed = {...refresh, state: "failed", error: "Stats calculation failed: ValueError"};\n'
        + "assert.deepEqual(statsCheck([sent, answer], null, failed), failed);\n"
    )


def test_refresh_age_uses_completion_time_and_failure_keeps_retry_available():
    script = (
        function_source("span")
        + function_source("statsCheckText")
        + function_source("statsCheck")
        + function_source("renderSync")
        + '\nconst assert = require("node:assert/strict");\n'
        + 'Date.now = () => 180000; const when = () => "old timestamp";\n'
        + 'assert.equal(statsCheckText({state:"refreshed",at:1,completed_at:120000}), "refreshed 1m ago");\n'
        + 'assert.equal(statsCheckText({state:"pending",at:60000}), "pending 2m ago");\n'
        + "const nodes = {}; const $ = id => nodes[id] ||= {};\n"
        + 'const lastSync = () => null, cooldown = () => "again in five minutes";\n'
        + "const statsSent = null;\n"
        + 'let meta = {stats_refresh:{id:"failed",state:"failed",at:1,completed_at:120000,error:"Stats calculation failed: ValueError"}};\n'
        + "renderSync();\n"
        + 'assert.equal(nodes["stats-state"].textContent, "failed 1m ago");\n'
        + 'assert.equal(nodes["stats-state"].title, "Stats calculation failed: ValueError");\n'
        + 'assert.equal(nodes["stats-sync"].disabled, false);\n'
        + 'assert.match(nodes["stats-sync"].ariaLabel, /Retry stats refresh/);\n'
    )
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


def test_the_header_shows_the_check_beside_the_refresh_button():
    page = page_source()
    assert '<span class="sync-state end" id="stats-state"></span><button class="mini-sync" id="stats-sync"' in page
    render = function_source("renderSync")
    assert "statsCheck(meta.events || [], statsSent, meta.stats_refresh, meta.time_left)" in render
    assert 'const checked = check ? `Stats check ${statsCheckText(check)}` : "";' in render
    assert '$("stats-sync").ariaLabel = [checked, ' in render
    assert '$("stats-state").textContent = check ? statsCheckText(check) : "";' in render


def test_the_stats_card_has_no_stats_check_row():
    script = (
        "const nodes = {}; const $ = id => nodes[id] ||= {replaceChildren: (...rows) => {nodes[id].textContent = rows.join(' · ');}};\n"
        "const h = (tag, attrs, ...children) => attrs.text || children.filter(Boolean).join(' ');\n"
        "const when = () => 'now'; const span = () => '1m'; const inScope = list => list; const activeAgents = () => 0;\n"
        "const swarm = null; const inboxPending = () => 0;\n"
        + EVENTS
        + "let meta = {created_at: 1, events: [sent, answer]};\n"
        "let doc = {phases: [], followups: [], questions: [], tasks: [], time_left_minutes: null, chat: [{id: 'm3', by: 'boss', at: 150, text: 'ok'}]};\n"
        "const statsSent = null;\n"
        + function_source("statsCheck")
        + function_source("statsCheckText")
        + function_source("timeLeftInputs")
        + function_source("renderStats")
        + "\nconst assert = require('node:assert/strict');\n"
        "renderStats();\n"
        "assert.match(statsCheckText(statsCheck(meta.events, statsSent)), /^pending .+ ago$/);\n"
        "assert.doesNotMatch(nodes.stats.textContent, /Stats check|answered|reply/);\n"
    )
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    assert "showReply" not in page_source()


def test_a_tick_refresh_newer_than_the_stats_check_sets_the_refresh_age():
    run(
        EVENTS
        + 'const refresh = {id: "s1", state: "refreshed", at: 100, completed_at: 120, calculation: {gap: "old"}};\n'
        + 'const ticked = {at: 900, calculation: {gap: ""}};\n'
        + "assert.deepEqual(statsCheck([sent], null, refresh, ticked),"
        + " {...refresh, completed_at: 900, calculation: ticked.calculation});\n"
        + "assert.deepEqual(statsCheck([sent], null, refresh, {at: 110}), refresh);\n"
        + 'const failed = {...refresh, state: "failed"};\n'
        + "assert.deepEqual(statsCheck([sent], null, failed, ticked), failed);\n"
        + "assert.deepEqual(statsCheck([], null, undefined, ticked),"
        + ' {state: "refreshed", at: 900, completed_at: 900, calculation: ticked.calculation});\n'
        + 'assert.deepEqual(statsCheck([sent], null, undefined, ticked), {state: "pending", at: 100});\n'
    )


def test_the_stats_card_counts_unfrozen_and_frozen_work_apart():
    script = (
        "const nodes = {}; const $ = id => nodes[id] ||= {replaceChildren: (...rows) => {nodes[id].rows = rows;}};\n"
        "const h = (tag, attrs, ...children) => [attrs.text, ...children].filter(Boolean).join(' ');\n"
        "const when = () => 'now'; const inScope = list => list; const activeAgents = () => 0;\n"
        "const swarm = null; const inboxPending = () => 0;\n"
        "let meta = {created_at: 1, events: [],"
        " time_left: {at: 5, calculation: {remaining: 2, frozen: 3, frozen_work: 90}}};\n"
        "let doc = {phases: [], followups: [], questions: [], tasks: [], time_left_minutes: 30};\n"
        + function_source("span")
        + function_source("timeLeftInputs")
        + function_source("renderStats")
        + "\nconst assert = require('node:assert/strict');\n"
        "renderStats();\n"
        "assert.deepEqual(nodes.stats.rows.slice(3, 6), ['Time left 30m', 'Unfrozen 2', 'Frozen 3 · 1h 30m of work']);\n"
        "meta = {created_at: 1, events: []};\n"
        "renderStats();\n"
        "assert.deepEqual(nodes.stats.rows.slice(4, 6), ['Unfrozen —', 'Frozen —']);\n"
    )
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
