import json
import subprocess
from pathlib import Path


def test_agent_cards_display_profile_and_model_as_text():
    page = (Path(__file__).resolve().parents[2] / "scripts/swarm_ledger/template.html").read_text()
    functions = []
    for name in ("span", "swarmCards", "swarmCard"):
        functions.append(
            "function " + name + "(" + page.split("  function " + name + "(", 1)[1].split("\n  }\n", 1)[0] + "\n}"
        )
    js = (
        "\n".join(functions)
        + """
const doc = {tasks: []};
function h(tag, attrs, ...children) { return {tag, attrs, children: children.filter(Boolean)}; }
function collapsible() {}
const raw = {agents: [
  {name: 'm', lane: 'master', profile: 'master', model: 'sonnet', effort: 'low', model_source: '<jev>', model_confidence: 0.72},
  {name: 'e', lane: 'eng', profile: '<qa>', model: 'gpt-6-luna'},
  {name: 'old', lane: 'ci'}
]};
const cards = swarmCards(raw, [], {}, 0);
console.log(JSON.stringify({cards, trees: cards.map(swarmCard)}));
"""
    )
    output = json.loads(subprocess.check_output(["node", "-e", js], text=True))
    assert [c["profile"] for c in output["cards"]] == ["master", "<qa>", "unknown"]

    def texts(node):
        return [node["attrs"].get("text")] + [t for child in node["children"] for t in texts(child)]

    assert "profile master" in texts(output["trees"][0])
    assert "model sonnet low" in texts(output["trees"][0])
    assert "source <jev>" in texts(output["trees"][0])
    assert "confidence 72%" in texts(output["trees"][0])
    assert "profile <qa>" in texts(output["trees"][1])
    assert "model gpt-6-luna" in texts(output["trees"][1])
    assert "profile unknown" in texts(output["trees"][2])
    assert "model unknown" in texts(output["trees"][2])
