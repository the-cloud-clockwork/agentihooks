import json
import subprocess
from pathlib import Path


def test_agent_card_shows_waiting_and_prompt_as_text():
    page = (Path(__file__).parents[2] / "scripts/swarm_ledger/template.html").read_text()
    functions = [
        "function " + name + "(" + page.split("  function " + name + "(", 1)[1].split("\n  }\n", 1)[0] + "\n}"
        for name in ("span", "swarmCards", "swarmCard")
    ]
    js = (
        "\n".join(functions)
        + """
const doc = {tasks: []};
function h(tag, attrs, ...children) { return {tag, attrs, children: children.filter(Boolean)}; }
function collapsible() {}
const cards = swarmCards({agents: [{name: 'engineer', lane: 'eng', status: 'waiting',
  input_prompt: 'Allow external CLAUDE.md file imports?'}]}, [], {}, 0);
console.log(JSON.stringify(swarmCard(cards[0])));
"""
    )
    tree = json.loads(subprocess.check_output(["node", "-e", js], text=True))

    def texts(node):
        return [node["attrs"].get("text")] + [text for child in node["children"] for text in texts(child)]

    assert "Waiting on input" in texts(tree)
    assert "Prompt: Allow external CLAUDE.md file imports?" in texts(tree)
