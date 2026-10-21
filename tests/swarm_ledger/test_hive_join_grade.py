import json
import subprocess

from tests.swarm_ledger.ledger_page import page_source


def function(page, name):
    return "function " + name + "(" + page.split("  function " + name + "(", 1)[1].split("\n  }\n", 1)[0] + "\n}"


def test_agent_rows_carry_join_report_and_render_grade_and_each_miss():
    page = page_source()
    names = ("span", "modelText", "terminalView", "executionView", "agentRows", "joinCell")
    script = "\n".join(function(page, name) for name in names)
    script += """
function h(tag, attrs, ...children) {return {tag, attrs, children};}
function label(text, tone) {return {text, tone};}
const report = {grade: 'partial', heartbeat_at: 1000, misses: {brain: {required: false}}};
const row = agentRows({agents: [{name: 'engineer', lane: 'eng', hive_join: report}]}, 2000)[0];
console.log(JSON.stringify({report: row.hiveJoin, cell: joinCell(row.hiveJoin)}));
"""
    result = json.loads(subprocess.check_output(["node", "-e", script], text=True))
    assert result["report"] == {"grade": "partial", "heartbeat_at": 1000, "misses": {"brain": {"required": False}}}
    text = json.dumps(result["cell"])
    assert "partial" in text
    assert "brain" in text
    assert "optional" in text
