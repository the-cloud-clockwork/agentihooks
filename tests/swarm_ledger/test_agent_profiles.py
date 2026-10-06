import json
import subprocess
from pathlib import Path


def test_agent_rows_show_the_model_and_effort_and_codex_by_harness():
    page = (Path(__file__).resolve().parents[2] / "scripts/swarm_ledger/template.html").read_text()
    functions = []
    for name in ("span", "modelText", "agentRows"):
        functions.append(
            "function " + name + "(" + page.split("  function " + name + "(", 1)[1].split("\n  }\n", 1)[0] + "\n}"
        )
    js = (
        "\n".join(functions)
        + """
const raw = {agents: [
  {name: 'e', lane: 'eng', harness: 'claude', model: 'claude-opus-5-5', effort: 'high', task: 't1'},
  {name: 'm', lane: 'master', harness: 'claude', model: 'sonnet', effort: 'low'},
  {name: 'x', lane: 'eng', harness: 'codex', model: 'gpt-6-luna', effort: 'high'},
  {name: 'old', lane: 'ci'}
]};
console.log(JSON.stringify(agentRows(raw, 0)));
"""
    )
    rows = json.loads(subprocess.check_output(["node", "-e", js], text=True))
    assert [(r["name"], r["model"]) for r in rows] == [
        ("m", "sonnet low"),
        ("e", "opus high"),
        ("x", "codex"),
        ("old", "—"),
    ]
    assert [(r["lane"], r["task"], r["state"]) for r in rows[:2]] == [("—", "", "live"), ("eng", "t1", "working")]
