import json
import re
import subprocess

import new_ledger
import pytest

from tests.swarm_ledger.test_swarm_panel import function_source


@pytest.fixture
def page_script():
    content = {"title": "Share", "overview": "o", "sources": [], "phases": [], "questions": [], "followups": []}
    page = new_ledger.render(new_ledger.build_doc(content), "share", 8765)
    assert re.search(r'<label for="cap-codex">codex .*?data-swarm="codex_down"', page)
    assert re.search(r'<input[^>]*min="0"[^>]*max="100"[^>]*step="5"[^>]*id="cap-codex"', page)
    assert 'aria-label="Codex share percent"' in page
    click = page.split('  $("swarm-box").addEventListener("click",', 1)[1].split("\n  });", 1)[0]
    functions = "\n".join(
        function_source(n)
        for n in (
            "renderPlanShape",
            "renderSwarm",
            "renderControls",
            "swarmControls",
            "doctorControls",
            "capBounds",
            "crewShown",
            "autonomyText",
        )
    )
    return (
        'let swarm = null, codexDraft = null, pending = "", armed = "", doc = null; const meta = {crew: []};'
        "const FIGURES = []; const nodes = {}; let sent = []; let click;"
        'const $ = id => nodes[id] ||= {value: "", textContent: "", contains: () => true, replaceChildren: () => {},'
        "querySelectorAll: () => buttons}; const h = () => ({}); const document = {};"
        "const swarmCards = () => []; const swarmCard = () => ({});"
        "const restoreCards = () => []; const restoreCard = () => ({}); const transferCard = () => ({});"
        'const buttons = ["codex_down", "codex_up", "set"].map(action => ({dataset: {swarm: action},'
        "classList: {toggle: () => {}}, setAttribute: () => {}}));"
        "const swarmControl = body => {sent.push(body)};"
        + functions
        + f"\nclick = {click}}};\n"
        + 'const sw = {config: {max_eng: 2, max_ci: 1, codex_share: 35, state: "running"}, tasks: {}, agents: [], spawns: {codex: 2, claude: 5}};'
        + "const press = action => click({target: {closest: () => buttons.find(b => b.dataset.swarm === action)}});"
    )


def run_page(script, expression):
    result = subprocess.run(["node", "-e", script + expression], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def test_page_shows_current_share_and_live_split(page_script):
    result = run_page(
        page_script,
        'renderSwarm(sw); console.log(JSON.stringify([$("cap-codex").value, $("codex-split").textContent]));',
    )
    assert result == [35, "2/7 spawns · 28% actual"]


def test_codex_steps_are_five_and_set_caps_sends_the_draft(page_script):
    result = run_page(
        page_script,
        'renderSwarm(sw); press("codex_up"); renderSwarm(sw); const up = $("cap-codex").value;'
        'press("codex_up"); press("codex_down"); const down = $("cap-codex").value; press("set");'
        "console.log(JSON.stringify({up, down, sent}));",
    )
    assert result == {"up": 40, "down": 40, "sent": [{"action": "set", "max_eng": 2, "max_ci": 1, "codex_share": 40}]}


@pytest.mark.parametrize(
    "share, action, disabled, expected",
    [
        (0, "codex_up", "codex_down", 5),
        (100, "codex_down", "codex_up", 95),
        (98, "codex_up", "codex_down", 100),
        (2, "codex_down", "codex_up", 0),
    ],
)
def test_codex_step_bounds(page_script, share, action, disabled, expected):
    result = run_page(
        page_script,
        f'sw.config.codex_share = {share}; renderSwarm(sw); const before = buttons.find(b => b.dataset.swarm === "{disabled}").disabled;'
        f'press("{action}"); console.log(JSON.stringify([before, $("cap-codex").value, sent]));',
    )
    assert result == [share in (0, 100), expected, []]


def test_no_spawns_shows_zero_actual_percent(page_script):
    assert (
        run_page(
            page_script, 'sw.spawns = {}; renderSwarm(sw); console.log(JSON.stringify($("codex-split").textContent));'
        )
        == "0/0 spawns · 0% actual"
    )
