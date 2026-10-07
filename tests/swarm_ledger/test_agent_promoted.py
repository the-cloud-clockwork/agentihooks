import json
import subprocess
from pathlib import Path

PAGE = Path(__file__).parents[2] / "scripts/swarm_ledger/template.html"


def _rows(agents):
    page = PAGE.read_text()
    functions = [
        "function " + name + "(" + page.split("  function " + name + "(", 1)[1].split("\n  }\n", 1)[0] + "\n}"
        for name in ("span", "modelText", "agentRows")
    ]
    js = "\n".join(functions) + f"\nconsole.log(JSON.stringify(agentRows({{agents: {json.dumps(agents)}}}, 0)));"
    return json.loads(subprocess.check_output(["node", "-e", js], text=True))


def test_a_promoted_engineer_row_is_marked_promoted():
    rows = _rows(
        [
            {"name": "engineer@a1b2c3-0001", "lane": "eng", "status": "working", "promoted": True},
            {"name": "engineer@a1b2c3-0002", "lane": "eng", "status": "working", "promoted": False},
        ]
    )
    assert [(r["name"], r["promoted"]) for r in rows] == [
        ("engineer@a1b2c3-0001", True),
        ("engineer@a1b2c3-0002", False),
    ]


def test_the_state_cell_shows_the_promoted_label():
    page = PAGE.read_text()
    assert (
        'a.promoted ? h("span", { class: "sw-promoted" }, label(a.state), label("promoted")) : label(a.state)' in page
    )
    assert ".lbl.promoted {" in page
