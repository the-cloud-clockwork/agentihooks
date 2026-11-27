import inspect
import json
import os
import subprocess
import sys
from collections import Counter
from collections.abc import Collection
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.swarm_ledger import page_policy
from tests.swarm_ledger import page_replay
from tests.swarm_ledger.test_caps_columns import browser as chromium_browser
from tests.test_codeowners import _owners

browser = chromium_browser

ROOT = Path(__file__).resolve().parents[2]
STYLE_POLICY = next(part for part in page_policy.policy({}, ROOT, 0).split("; ") if part.startswith("style-src"))
BASE = (
    '<main id="main-content"><p id="keep">Notes</p><button id="drop">Old</button>'
    '<input id="drop-field" class="drop" value="x"><p id="after">After</p></main>'
)
HEAD = '<main id="main-content"><p id="keep">Notes</p><p id="after">After</p></main>'
REMOVED = {"#drop": 1, ".drop": 1}


def capture(browser: Any, body: str, removed: Collection[str] = ()) -> tuple[dict[str, int], str]:
    context = browser.new_context(viewport=page_replay.VIEWPORT)
    try:
        if removed:
            context.add_init_script(page_replay.removal(removed))
        page = context.new_page()
        headers = {"Content-Security-Policy": STYLE_POLICY}
        page.route(
            "http://replay.test/", lambda route: route.fulfill(content_type="text/html", body=body, headers=headers)
        )
        page.goto("http://replay.test/")
        return page_replay.seen(page, removed), json.dumps(page.evaluate(page_replay.CAPTURE))
    finally:
        context.close()


def test_without_a_contract_the_removal_stays_a_byte_difference(browser):
    assert capture(browser, BASE)[1] != capture(browser, HEAD)[1]


def test_a_contract_naming_each_removed_element_makes_base_and_head_identical(browser):
    seen, base = capture(browser, BASE, REMOVED)
    assert seen == REMOVED
    assert page_replay.mismatched(REMOVED, seen) == []
    assert base == capture(browser, HEAD)[1]


def test_a_contract_still_fails_any_difference_it_does_not_name(browser):
    _, base = capture(browser, BASE, REMOVED)
    assert base != capture(browser, HEAD.replace("After", "Changed"))[1]
    assert base != capture(browser, HEAD.replace('<p id="after">After</p>', ""))[1]


def test_a_contract_does_not_hide_an_element_the_head_still_shows(browser):
    _, base = capture(browser, BASE, REMOVED)
    assert base != capture(browser, BASE)[1]


def test_a_named_selector_the_base_page_never_shows_fails_the_contract(browser):
    contract = {**REMOVED, "#never": 1}
    seen, _ = capture(browser, BASE, contract)
    assert seen["#never"] == 0
    assert page_replay.mismatched(contract, seen) == [
        "#never names 1 elements but the most the base page shows at once is 0"
    ]


def test_a_selector_broader_than_its_named_count_fails_the_contract(browser):
    contract = {"main": 1, "p": 1}
    seen, _ = capture(browser, BASE, contract)
    assert page_replay.mismatched(contract, seen) == [
        "p names 1 elements but the most the base page shows at once is 2"
    ]


def contract(tmp_path, value):
    path = tmp_path / "expected_page_change.json"
    path.write_text(json.dumps(value))
    return path


VALID = {
    "reviewed_by": ["standards-reader", "spec-reader"],
    "removed": [
        {"selector": "#drop", "order": "Operator: remove the old button", "matches": 1},
        {"selector": ".drop", "order": "Operator: remove the old field", "matches": 1},
    ],
}


def test_load_contract_returns_the_named_selectors(tmp_path: Path) -> None:
    assert page_replay.load_contract(contract(tmp_path, VALID)) == (REMOVED, {})


@pytest.mark.parametrize(
    "value",
    [
        [],
        {"removed": VALID["removed"]},
        {**VALID, "reviewed_by": []},
        {**VALID, "reviewed_by": [""]},
        {**VALID, "removed": []},
        {**VALID, "removed": [{"selector": "#drop", "matches": 1}]},
        {**VALID, "removed": [{"selector": "#drop", "order": "Operator: remove"}]},
        {**VALID, "removed": [{"selector": "", "order": "Operator: remove", "matches": 1}]},
        {**VALID, "removed": [{"selector": "#drop", "order": "", "matches": 1}]},
        {**VALID, "removed": [{"selector": "#drop", "order": "Operator: remove", "matches": 0}]},
        {**VALID, "removed": [{"selector": "#drop", "order": "Operator: remove", "matches": True}]},
        {**VALID, "removed": [{"selector": "#drop", "order": "Operator: remove", "matches": 1, "note": "y"}]},
        {**VALID, "removed": [VALID["removed"][0], VALID["removed"][0]]},
        {**VALID, "extra": True},
    ],
)
def test_load_contract_refuses_a_malformed_contract(tmp_path, value):
    with pytest.raises(SystemExit):
        page_replay.load_contract(contract(tmp_path, value))


CONTRACT = "scripts/swarm_ledger/expected_page_change.json"


def page_step(
    tmp_path: Path, contracts: dict[str, str], fault: str = "fault"
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    for folder, page in (("base", "same"), ("head", "same"), ("fault-chat", fault)):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "page.json").write_text(page)
    for folder, text in contracts.items():
        (tmp_path / folder / CONTRACT).parent.mkdir(parents=True)
        (tmp_path / folder / CONTRACT).write_text(text)
    stub = tmp_path / "bin" / "python"
    stub.parent.mkdir()
    stub.write_text('#!/usr/bin/env bash\necho "$(basename "$3")" "${@:6}" >> "$CALLS"\ncp "$3/page.json" "$5"\n')
    stub.chmod(0o755)
    (tmp_path / "runner").mkdir()
    env = {
        **os.environ,
        "PATH": f"{stub.parent}{os.pathsep}{os.environ['PATH']}",
        "PWD": str(tmp_path),
        "RUNNER_TEMP": str(tmp_path / "runner"),
        "CALLS": str(tmp_path / "calls"),
    }
    script = str(ROOT / "tests/swarm_ledger/equivalence.sh")
    result = subprocess.run(
        ["bash", script, "page"], cwd=tmp_path, env=env, capture_output=True, text=True, check=False
    )
    return result, sorted((tmp_path / "calls").read_text().splitlines())


@pytest.mark.feature("page-equivalence-reviewed-additions")
@pytest.mark.parametrize(
    ("contracts", "flags"),
    [
        ({}, {}),
        ({"base": "{}", "head": "{}"}, {}),
        ({"head": "{}"}, {"base": "--remove", "head": "--add", "fault-chat": "--add"}),
        ({"base": "{}", "head": '{"added": []}'}, {"base": "--remove", "head": "--add", "fault-chat": "--add"}),
    ],
)
def test_the_page_step_gives_the_base_the_removed_list_and_the_head_and_fault_the_added_list(
    tmp_path: Path, contracts: dict[str, str], flags: dict[str, str]
) -> None:
    result, calls = page_step(tmp_path, contracts)
    head = tmp_path / "head" / CONTRACT
    expected = [
        f"{folder} {flags[folder]} {head}" if folder in flags else folder for folder in ("base", "head", "fault-chat")
    ]
    assert calls == sorted(expected)
    assert result.returncode == 0
    assert "PASS: identical base and head pages; planted chat fault rejected" in result.stdout


@pytest.mark.feature("page-equivalence-reviewed-additions")
def test_the_page_step_still_fails_a_fault_page_equal_to_the_base(tmp_path: Path) -> None:
    result, _ = page_step(tmp_path, {"head": "{}"}, fault="same")
    assert result.returncode == 1
    assert "FAIL: planted chat fault escaped the page equivalence check" in result.stdout


def test_the_expected_page_change_contract_has_a_code_owner():
    assert _owners("scripts/swarm_ledger/expected_page_change.json")


FOLDS = (
    "<script>localStorage.setItem('plan-ledger:replay:fold', JSON.stringify(Object.fromEntries("
    "[...document.querySelectorAll('details')].map((box) => [box.id, true]))))</script>"
)
SHOWN = (
    '<main id="main-content"><details id="notes-box"><summary>Notes</summary></details>'
    f'<p id="after">After</p></main>{FOLDS}'
)
ADDED = SHOWN.replace(
    '<p id="after">',
    '<section id="sec-orders"><details id="orders-box"><summary>Orders</summary></details></section><p id="after">',
)
PANEL = {"selector": "#sec-orders", "order": "Operator: add the orders panel", "matches": 1}
CALL = {"request": "GET /api/v1/ledgers/replay/orders", "order": "Operator: add the orders panel", "matches": 2}
NAMED = {"GET /api/v1/ledgers/replay/orders": 2}
ORDERS = {"method": "GET", "path": "/api/v1/ledgers/replay/orders", "type": "", "token": True, "body": None}


def save(chat: str) -> dict[str, Any]:
    return {
        "method": "PUT",
        "path": "/api/v1/ledgers/replay/saves",
        "type": "application/json",
        "token": True,
        "body": {"chat": chat},
    }


@pytest.mark.feature("page-equivalence-reviewed-additions")
def test_a_capture_keeps_every_stored_entry_of_the_elements_it_shows_as_written(browser: Any) -> None:
    spaced = '{"notes-box": true, "after": false}'
    body = SHOWN.replace("</main>", '<p id="drop">Old</p></main>')
    body += f"<script>localStorage.setItem('spaced', {json.dumps(spaced)})</script>"
    stored = {"plan-ledger:replay:fold": '{"notes-box":true}', "spaced": spaced}
    assert json.loads(capture(browser, body)[1])["storage"] == stored
    assert json.loads(capture(browser, body, {"#drop": 1})[1])["storage"] == stored


@pytest.mark.feature("page-equivalence-reviewed-additions")
def test_the_fake_api_records_each_request_and_hands_each_step_its_requests_sorted() -> None:
    api = page_replay.FakeApi({}, lambda doc: {})
    noted = [
        SimpleNamespace(
            url="http://127.0.0.1:9/api/v1/ledgers/replay/saves",
            method="PUT",
            post_data_buffer=b'{"chat": "hi"}',
            headers={"content-type": "application/json"},
        ),
        SimpleNamespace(
            url="http://127.0.0.1:9/api/v1/ledgers/replay/orders?limit=5",
            method="GET",
            post_data_buffer=None,
            headers={"x-ledger-token": "1"},
        ),
        SimpleNamespace(
            url="http://127.0.0.1:9/api/v1/ledgers/replay/uploads/media",
            method="POST",
            post_data_buffer=b"png",
            headers={"content-type": "image/png"},
        ),
    ]
    assert [api.note(request) for request in noted] == [
        "/api/v1/ledgers/replay/saves",
        "/api/v1/ledgers/replay/orders",
        "/api/v1/ledgers/replay/uploads/media",
    ]
    api.dialogs.append("Discard the draft?")
    upload = {
        "method": "POST",
        "path": "/api/v1/ledgers/replay/uploads/media",
        "type": "image/png",
        "token": False,
        "body": "3 bytes",
    }
    put = {**save("hi"), "token": False}
    assert api.take() == ([upload, ORDERS, put], ["Discard the draft?"])
    assert api.take() == ([], [])


@pytest.mark.feature("page-equivalence-reviewed-additions")
def test_a_contract_naming_each_added_element_makes_head_and_base_identical(browser: Any) -> None:
    seen, head = capture(browser, ADDED, {"#sec-orders": 1})
    assert head == capture(browser, SHOWN)[1]
    assert page_replay.mismatched({"#sec-orders": 1}, seen, "head") == []


@pytest.mark.feature("page-equivalence-reviewed-additions")
def test_an_added_element_the_contract_does_not_name_still_fails(browser: Any) -> None:
    unnamed = ADDED.replace('<p id="after">', '<p id="extra">New</p><p id="after">')
    assert capture(browser, unnamed, {"#sec-orders": 1})[1] != capture(browser, SHOWN)[1]


@pytest.mark.feature("page-equivalence-reviewed-additions")
def test_naming_an_element_the_base_also_shows_cannot_hide_a_change_to_it(browser: Any) -> None:
    changed = ADDED.replace("<summary>Notes</summary>", "<summary>Changed</summary>")
    assert capture(browser, changed, {"#sec-orders": 1, "#notes-box": 1})[1] != capture(browser, SHOWN)[1]


@pytest.mark.feature("page-equivalence-reviewed-additions")
def test_added_counts_the_head_page_does_not_match_fail_naming_the_head(browser: Any) -> None:
    named = {"#sec-orders": 1, "#never": 1, "details": 1}
    seen, _ = capture(browser, ADDED, named)
    assert page_replay.mismatched(named, seen, "head") == [
        "#never names 1 elements but the most the head page shows at once is 0",
        "details names 1 elements but the most the head page shows at once is 2",
    ]
    assert page_replay.miscounted(NAMED, Counter({"GET /api/v1/ledgers/replay/orders": 3}), "head") == [
        "GET /api/v1/ledgers/replay/orders names 2 requests but the head page makes 3"
    ]


@pytest.mark.feature("page-equivalence-reviewed-additions")
def test_named_requests_are_dropped_and_counted_while_the_planted_chat_fault_still_differs() -> None:
    made: Counter[str] = Counter()
    chat = save("@engineer@replay-2 please rebase")
    head = page_replay.unnamed([ORDERS, chat], NAMED, made) + page_replay.unnamed([ORDERS], NAMED, made)
    fault = page_replay.unnamed([ORDERS, save("please rebase")], NAMED, Counter())
    assert head == [chat]
    assert fault == [save("please rebase")]
    assert fault != head
    others = [{**ORDERS, "path": "/api/v1/ledgers/replay/orders/o1"}, {**ORDERS, "method": "POST"}]
    assert page_replay.unnamed(others, NAMED, made) == others
    assert made == Counter({"GET /api/v1/ledgers/replay/orders": 2})
    assert page_replay.miscounted(NAMED, made, "head") == []
    saves = {**NAMED, "PUT /api/v1/ledgers/replay/saves": 1}
    assert page_replay.unnamed([ORDERS, chat], saves, Counter()) != [chat]


@pytest.mark.feature("page-equivalence-reviewed-additions")
@pytest.mark.parametrize(
    ("value", "removed", "added"),
    [
        (VALID, (REMOVED, {}), ({}, {})),
        ({"reviewed_by": ["spec-reader"], "added": [PANEL, CALL]}, ({}, {}), ({"#sec-orders": 1}, NAMED)),
        ({**VALID, "added": [PANEL, VALID["removed"][0]]}, (REMOVED, {}), ({"#sec-orders": 1, "#drop": 1}, {})),
        ({**VALID, "removed": [*VALID["removed"], CALL]}, (REMOVED, NAMED), ({}, {})),
    ],
)
def test_load_contract_returns_the_selectors_and_requests_of_each_list(
    tmp_path: Path,
    value: dict[str, Any],
    removed: tuple[dict[str, int], dict[str, int]],
    added: tuple[dict[str, int], dict[str, int]],
) -> None:
    path = contract(tmp_path, value)
    assert page_replay.load_contract(path) == removed
    assert page_replay.load_contract(path, "added") == added


@pytest.mark.feature("page-equivalence-reviewed-additions")
@pytest.mark.parametrize(
    "value",
    [
        {"reviewed_by": ["spec-reader"]},
        {**VALID, "added": []},
        {**VALID, "added": PANEL},
        {**VALID, "renamed": [PANEL]},
        {**VALID, "added": [{**PANEL, **CALL}]},
        {**VALID, "added": [{"order": "Operator: add the orders panel", "matches": 1}]},
        {**VALID, "added": [{**PANEL, "selector": " "}]},
        {**VALID, "added": [{**CALL, "order": " "}]},
        {**VALID, "added": [{**CALL, "matches": 0}]},
        {**VALID, "added": [{**CALL, "matches": "2"}]},
        {**VALID, "added": [{**CALL, "request": "/api/v1/ledgers/replay/orders"}]},
        {**VALID, "added": [{**CALL, "request": "get /api/v1/ledgers/replay/orders"}]},
        {**VALID, "added": [{**CALL, "request": "GET api/v1/ledgers/replay/orders"}]},
        {**VALID, "added": [{**CALL, "request": "GET /api/v1/ledgers/replay/orders?limit=5"}]},
        {**VALID, "added": [{**CALL, "request": "GET  /api/v1/ledgers/replay/orders"}]},
        {**VALID, "added": [PANEL, PANEL]},
        {**VALID, "added": [CALL, {**CALL, "order": "Operator: poll the orders"}]},
    ],
)
def test_load_contract_refuses_a_malformed_added_list(tmp_path: Path, value: object) -> None:
    with pytest.raises(SystemExit):
        page_replay.load_contract(contract(tmp_path, value))


@pytest.mark.feature("page-equivalence-reviewed-additions")
@pytest.mark.parametrize(
    ("flag", "hidden", "named", "side"),
    [("--remove", REMOVED, {}, "base"), ("--add", {"#sec-orders": 1}, NAMED, "head")],
)
def test_main_hides_the_list_its_flag_names_in_that_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    flag: str,
    hidden: dict[str, int],
    named: dict[str, int],
    side: str,
) -> None:
    path = contract(tmp_path, {**VALID, "added": [PANEL, CALL]})
    signature = inspect.signature(page_replay.record)
    calls: list[dict[str, object]] = []

    def record(*args: object, **kwargs: object) -> list[object]:
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        calls.append(dict(bound.arguments))
        return []

    monkeypatch.setattr(page_replay, "record", record)
    output = tmp_path / "page.json"
    monkeypatch.setattr(sys, "argv", ["page_replay.py", str(tmp_path), "ledger", str(output), flag, str(path)])
    page_replay.main()
    expected = {"root": tmp_path.resolve(), "folder": Path("ledger"), "hidden": hidden, "named": named, "side": side}
    assert calls == [expected]
    assert output.read_text() == "[]\n"
