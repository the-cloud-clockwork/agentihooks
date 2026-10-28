import json
from pathlib import Path

import pytest

from tests.swarm_ledger import page_replay
from tests.swarm_ledger.test_caps_columns import browser as chromium_browser

browser = chromium_browser

ROOT = Path(__file__).resolve().parents[2]
BASE = (
    '<main id="main-content"><p id="keep">Notes</p><button id="drop">Old</button>'
    '<input id="drop-field" class="drop" value="x"><p id="after">After</p></main>'
)
HEAD = '<main id="main-content"><p id="keep">Notes</p><p id="after">After</p></main>'
REMOVED = ("#drop", ".drop")


def capture(browser, body, removed=()):
    context = browser.new_context(viewport=page_replay.VIEWPORT)
    try:
        if removed:
            context.add_init_script(page_replay.removal(removed))
        page = context.new_page()
        page.route("http://replay.test/", lambda route: route.fulfill(content_type="text/html", body=body))
        page.goto("http://replay.test/")
        return page_replay.seen(page, removed), json.dumps(page.evaluate(page_replay.CAPTURE))
    finally:
        context.close()


def test_without_a_contract_the_removal_stays_a_byte_difference(browser):
    assert capture(browser, BASE)[1] != capture(browser, HEAD)[1]


def test_a_contract_naming_each_removed_element_makes_base_and_head_identical(browser):
    seen, base = capture(browser, BASE, REMOVED)
    assert seen == set(REMOVED)
    assert base == capture(browser, HEAD)[1]


def test_a_contract_still_fails_any_difference_it_does_not_name(browser):
    _, base = capture(browser, BASE, REMOVED)
    assert base != capture(browser, HEAD.replace("After", "Changed"))[1]
    assert base != capture(browser, HEAD.replace('<p id="after">After</p>', ""))[1]


def test_a_contract_does_not_hide_an_element_the_head_still_shows(browser):
    _, base = capture(browser, BASE, REMOVED)
    assert base != capture(browser, BASE)[1]


def test_a_named_selector_the_base_page_never_shows_is_reported_unseen(browser):
    seen, _ = capture(browser, BASE, (*REMOVED, "#never"))
    assert "#never" not in seen


def contract(tmp_path, value):
    path = tmp_path / "expected_page_change.json"
    path.write_text(json.dumps(value))
    return path


VALID = {
    "reviewed_by": ["standards-reader", "spec-reader"],
    "removed": [
        {"selector": "#drop", "order": "Operator: remove the old button"},
        {"selector": ".drop", "order": "Operator: remove the old field"},
    ],
}


def test_load_contract_returns_the_named_selectors(tmp_path):
    assert page_replay.load_contract(contract(tmp_path, VALID)) == ("#drop", ".drop")


@pytest.mark.parametrize(
    "value",
    [
        [],
        {"removed": VALID["removed"]},
        {**VALID, "reviewed_by": []},
        {**VALID, "reviewed_by": [""]},
        {**VALID, "removed": []},
        {**VALID, "removed": [{"selector": "#drop"}]},
        {**VALID, "removed": [{"selector": "", "order": "Operator: remove"}]},
        {**VALID, "removed": [{"selector": "#drop", "order": ""}]},
        {**VALID, "removed": [VALID["removed"][0], VALID["removed"][0]]},
        {**VALID, "extra": True},
    ],
)
def test_load_contract_refuses_a_malformed_contract(tmp_path, value):
    with pytest.raises(SystemExit):
        page_replay.load_contract(contract(tmp_path, value))


def test_the_page_grader_hides_named_elements_only_in_the_base_replay_of_a_changed_contract():
    script = (ROOT / "tests/swarm_ledger/equivalence.sh").read_text()
    page = script.split("\npage() {\n", 1)[1].split("\n}\n", 1)[0]
    assert "contract=scripts/swarm_ledger/expected_page_change.json" in page
    assert '! cmp -s "head/$contract" "base/$contract"' in page
    [base] = [line for line in page.splitlines() if line.strip().startswith("replay base-page ")]
    assert base.rstrip(" &").endswith('base-page.json "${remove[@]}"')
    others = [
        line for line in page.splitlines() if line.strip().startswith(("replay head-page ", "replay fault-page "))
    ]
    assert len(others) == 2 and all("remove" not in line for line in others)
