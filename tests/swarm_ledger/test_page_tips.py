import json
import re
import urllib.request

import pytest

from hooks.context import profile_chain
from scripts.gates import claims, quiet, reruns
from scripts.swarm_ledger import ledger_artifacts, ledger_bin, ledger_phases, page_tips
from scripts.swarm_ledger import ledger_server as server
from tests.swarm_ledger.ledger_page import MODULES, served
from tests.swarm_ledger.test_caps_columns import browser as chromium_browser

browser = chromium_browser
TOOLTIPS = MODULES.parents[1] / "tooltips.js"
NUMBER_WORDS = r"\b(two|three|four|five|ten|twenty|thirty|sixty)\b"


def test_the_tip_values_are_the_numbers_the_rules_enforce():
    assert page_tips.values() == {
        "send_backs": ledger_phases.ROUND_CAP,
        "claims": claims.CAP,
        "reruns": reruns.RERUN_CAP,
        "quiet_minutes": quiet.QUIET_MINUTES,
        "overlays": profile_chain.OVERLAY_CAP,
        "bin_days": ledger_bin.KEEP_DAYS,
        "trash_days": ledger_artifacts.KEEP_DAYS,
    }


def test_the_rerun_gate_spends_the_cap_the_tips_quote():
    assert reruns.RerunBudget().cap == reruns.RERUN_CAP == 2


def test_version_one_serves_the_tip_values():
    with served(server) as base:
        with urllib.request.urlopen(f"{base}/api/v1/tips", timeout=5) as resp:
            assert resp.status == 200
            assert json.loads(resp.read()) == {"data": page_tips.values()}


def test_no_tooltip_bakes_a_rule_number():
    tips = TOOLTIPS.read_text(encoding="utf-8").split("const TIPS = [", 1)[1].split("];", 1)[0]
    assert not re.findall(NUMBER_WORDS, tips, re.I)
    assert not re.findall(r"\d", tips)
    assert set(re.findall(r"\{(\w+)\}", tips)) <= set(page_tips.values())
    assert "30 days" not in (MODULES.parents[1] / "shell.html").read_text(encoding="utf-8")
    assert "const OVERLAY_CAP = 3" not in (MODULES / "swarm.js").read_text(encoding="utf-8")


def test_the_page_and_home_read_the_tips_through_the_shared_api_module():
    api = (MODULES / "api.js").read_text(encoding="utf-8")
    assert "export function readTips()" in api
    assert "export async function loadTips()" in api
    assert '"/api/v1/tips"' in (MODULES / "config.js").read_text(encoding="utf-8")
    assert "loadTips()" in (MODULES / "main.js").read_text(encoding="utf-8")
    assert "loadTips()" in (MODULES.parent / "home" / "home.js").read_text(encoding="utf-8")


@pytest.fixture
def tab(browser):
    context = browser.new_context(viewport={"width": 1920, "height": 1080})
    page = context.new_page()
    page.set_content(
        '<button id="bin-fab" type="button">bin</button><button class="act del" type="button">del</button>'
    )
    page.add_script_tag(content=TOOLTIPS.read_text(encoding="utf-8"))
    yield page
    context.close()


def test_a_tip_waits_for_its_values_and_then_quotes_them(tab):
    read = "(id) => window.ledgerTip(document.querySelector(id))"
    assert tab.evaluate(read, "#bin-fab") is None
    tab.evaluate("(values) => window.ledgerTipValues(values)", page_tips.values())
    assert f"{ledger_bin.KEEP_DAYS} days" in tab.evaluate(read, "#bin-fab")
    assert f"{ledger_bin.KEEP_DAYS} days" in tab.evaluate(read, ".act.del")
