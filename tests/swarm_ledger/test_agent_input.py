import json
import subprocess

from tests.swarm_ledger.ledger_page import page_source


def test_an_agent_at_a_startup_prompt_shows_waiting_in_its_row():
    page = page_source()
    functions = [
        "function " + name + "(" + page.split("  function " + name + "(", 1)[1].split("\n  }\n", 1)[0] + "\n}"
        for name in ("span", "modelText", "terminalView", "executionView", "agentRows")
    ]
    constants = [
        line.strip() for line in page.splitlines() if line.startswith(("  const WAIT_TEXT", "  const OBSERVED_TONES"))
    ]
    js = (
        "\n".join(constants + functions)
        + """
const rows = agentRows({agents: [{name: 'engineer', lane: 'eng', status: 'waiting',
  input_prompt: 'Allow external CLAUDE.md file imports?'}], executions: {engineer: {backend: 'local', generation: 1,
  pool: '', observed: 'waiting', wait: 'input', seen_at: 0, terminal_behind: false}}}, 0);
console.log(JSON.stringify(rows));
"""
    )
    rows = json.loads(subprocess.check_output(["node", "-e", js], text=True))
    assert rows[0]["state"] == "waiting"
    assert rows[0]["execution"] == {
        "shown": "input wait",
        "tone": "waiting",
        "seen": "not seen",
        "where": "local · gen 1",
        "behind": False,
    }
