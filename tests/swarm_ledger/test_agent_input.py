import json
import subprocess
from pathlib import Path


def test_an_agent_at_a_startup_prompt_shows_waiting_in_its_row():
    page = (Path(__file__).parents[2] / "scripts/swarm_ledger/template.html").read_text()
    functions = [
        "function " + name + "(" + page.split("  function " + name + "(", 1)[1].split("\n  }\n", 1)[0] + "\n}"
        for name in ("span", "modelText", "agentRows")
    ]
    js = (
        "\n".join(functions)
        + """
const rows = agentRows({agents: [{name: 'engineer', lane: 'eng', status: 'waiting',
  input_prompt: 'Allow external CLAUDE.md file imports?'}]}, 0);
console.log(JSON.stringify(rows));
"""
    )
    rows = json.loads(subprocess.check_output(["node", "-e", js], text=True))
    assert rows[0]["state"] == "waiting"
