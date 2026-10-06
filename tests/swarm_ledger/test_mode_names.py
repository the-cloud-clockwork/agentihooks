from scripts.gates.catalog import defaults
from tests.swarm_ledger.test_swarm_layout import ROOT, browser, open_page, status  # noqa: F401


def test_every_gate_shows_failure_outcomes_and_check_tooltips(open_page):  # noqa: F811
    page = open_page(status(gate_modes=defaults()), width=1920)
    page.tab.add_script_tag(path=str(ROOT / "scripts/swarm_ledger/tooltips.js"))
    page.tab.set_viewport_size({"width": 1920, "height": 1080})
    for name in defaults():
        group = page.tab.get_by_role("group", name=f"{name} gate mode", exact=True)
        assert group.locator("button").all_text_contents() == ["deny", "log only", "skip"]
        assert group.locator("button").evaluate_all("buttons => buttons.every(b => b.scrollWidth <= b.clientWidth)")
        tip = group.locator("[data-gate-name]").evaluate("el => window.ledgerTip(el)")
        assert tip and not any(word in tip.lower() for word in ("refuses", "holds", "blocks", "fails"))
    tips = (
        page.tab.locator(".sw-gate")
        .first.locator("button")
        .evaluate_all("buttons => buttons.map(el => window.ledgerTip(el))")
    )
    assert tips == [
        "When the check fails, refuse the call and log the denial.",
        "When the check fails, let the call through and log a would be denial.",
        "Skip the check. It does not run or log a decision.",
    ]
