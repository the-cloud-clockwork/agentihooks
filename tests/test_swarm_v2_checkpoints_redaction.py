import json

import pytest

from scripts.swarm_v2.checkpoints import redaction

pytestmark = pytest.mark.unit

TOKEN = "ghp_" + "Z9" * 18
OTHER = "ghs_" + "A1" * 18
WORD = "pass" + "word"
KEY = "api" + "_key"
HIDDEN = "[REDACTED:github_token]"
GENERIC = "[REDACTED:generic_secret]"


def compact(record: dict) -> str:
    return json.dumps(record, separators=(",", ":"))


CASES = {
    "value": (json.dumps({"t": f"ran {TOKEN}"}) + "\n", compact({"t": f"ran {HIDDEN}"}) + "\n", 1),
    "keyed": (json.dumps({KEY: "abcd" + "1234efgh"}) + "\n", compact({KEY: GENERIC}) + "\n", 1),
    "keyed and own": (
        json.dumps({WORD: "hunter2hunter2 " + TOKEN}) + "\n",
        compact({WORD: f"{GENERIC} {HIDDEN}"}) + "\n",
        2,
    ),
    "own before keyed": (json.dumps({WORD: TOKEN + " hunter2hunter2"}) + "\n", compact({WORD: GENERIC}) + "\n", 1),
    "keyed merged with own": (json.dumps({KEY: "plain1234" + TOKEN}) + "\n", compact({KEY: GENERIC}) + "\n", 1),
    "keyed beside an own match of the same pattern": (
        json.dumps({KEY: "hunter2hunter2 " + WORD + "=abcd1234efgh"}) + "\n",
        compact({KEY: f"{GENERIC} {GENERIC}"}) + "\n",
        2,
    ),
    "keyed after an own match of the same pattern": (
        json.dumps({KEY: WORD.upper() + "=abcd1234efgh and hunter2hunter2"}) + "\n",
        compact({KEY: GENERIC}) + "\n",
        1,
    ),
    "keyed beside a marker already in the value": (
        json.dumps({WORD: GENERIC + " hunter2hunter2"}) + "\n",
        compact({WORD: GENERIC}) + "\n",
        0,
    ),
    "quoted assignment in an array line": (
        json.dumps([WORD.upper() + '="hunter2hunter2"']) + "\n",
        compact([GENERIC + '"']) + "\n",
        1,
    ),
    "keyword two levels up": (
        json.dumps({WORD: {"value": "hunter2hunter2", "n": [1]}}) + "\n",
        compact({WORD: {"value": "[REDACTED:nested_secret]", "n": [1]}}) + "\n",
        1,
    ),
    "keyword three levels up": (
        json.dumps({"a": {WORD: {"value": "hunter2hunter2"}}}) + "\n",
        compact({"a": {WORD: {"value": "[REDACTED:nested_secret]"}}}) + "\n",
        1,
    ),
    "escaped in an array line": ('["\\u0067hp_' + "Z9" * 18 + '", 1]\n', f'["{HIDDEN}",1]\n', 1),
    "escaped in a string line": ('"\\u0067hp_' + "Z9" * 18 + '"\n', f'"{HIDDEN}"\n', 1),
    "keyed despite nosecret": (
        json.dumps({WORD: "hunter2hunter2 # nosecret"}) + "\n",
        compact({WORD: GENERIC}) + "\n",
        1,
    ),
    "keyed list": (json.dumps({KEY: ["abcd" + "1234efgh", 7]}) + "\n", compact({KEY: [GENERIC, 7]}) + "\n", 1),
    "key": (json.dumps({TOKEN: [1, "x"]}) + "\r\n", compact({HIDDEN: [1, "x"]}) + "\r\n", 1),
    "space before the line end": (json.dumps({"t": TOKEN}) + " \t\n", compact({"t": HIDDEN}) + " \t\n", 1),
    "doubled carriage return": (json.dumps({"t": TOKEN}) + "\r\r\n", compact({"t": HIDDEN}) + "\r\r\n", 1),
    "carriage return at the end": (json.dumps({"t": TOKEN}) + "\r", compact({"t": HIDDEN}) + "\r", 1),
    "key with a number line": (
        "-----BEGIN RSA PRIVATE" + " KEY-----\nAAAA\n12345\nBBBB\n-----END RSA PRIVATE" + " KEY-----\n",
        "[REDACTED:private_key]\n",
        1,
    ),
    "colliding keys": (json.dumps({TOKEN: 1, OTHER: 2}) + "\n", compact({HIDDEN: 2}) + "\n", 2),
    "nested": (
        json.dumps({"items": [TOKEN, {"text": f"{TOKEN} and {TOKEN}"}], "n": 3}) + "\n",
        compact({"items": [HIDDEN, {"text": f"{HIDDEN} and {HIDDEN}"}], "n": 3}) + "\n",
        3,
    ),
    "escaped beside a text line": (
        '{"t":"\\u0067hp_' + "Z9" * 18 + '"}\n{broken\n',
        compact({"t": HIDDEN}) + "\n{broken\n",
        1,
    ),
    "blank line kept": (
        json.dumps({"t": TOKEN}) + '\n\n{"a": 1}\n',
        compact({"t": HIDDEN}) + '\n\n{"a": 1}\n',
        1,
    ),
    "no final newline": (json.dumps({"t": TOKEN}), compact({"t": HIDDEN}), 1),
    "lone surrogate escape": ('{"t":"\\ud800 ' + TOKEN + '"}\n', '{"t":"\\ud800 ' + HIDDEN + '"}\n', 1),
    "non ascii rewritten as escapes": (json.dumps({"t": f"é {TOKEN}"}) + "\n", '{"t":"\\u00e9 ' + HIDDEN + '"}\n', 1),
    "text": (f"token {TOKEN}\n{{broken\n", f"token {HIDDEN}\n{{broken\n", 1),
    "text lines before a record": (
        "plain\n{broken\n" + json.dumps({"t": TOKEN}) + "\n",
        "plain\n{broken\n" + compact({"t": HIDDEN}) + "\n",
        1,
    ),
    "marker already present": (f"[REDACTED:x] {TOKEN}\n", f"[REDACTED:x] {HIDDEN}\n", 1),
    "strict only pattern": ("xoxb-" + "1" * 12 + "\n", "[REDACTED:slack_token]\n", 1),
    "key across lines": (
        "-----BEGIN RSA PRIVATE" + " KEY-----\nabc\n-----END RSA PRIVATE" + " KEY-----\n",
        "[REDACTED:private_key]\n",
        1,
    ),
    "clean": ('{"type": "user"}\nplain\n', '{"type": "user"}\nplain\n', 0),
    "empty": ("", "", 0),
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_scrub_hides_every_match_and_counts_each_once(case):
    given, expected, count = CASES[case]
    assert redaction.scrub(given.encode()) == (expected.encode(), count)


@pytest.mark.parametrize("case", sorted(CASES))
def test_a_second_scrub_changes_nothing(case):
    once, _ = redaction.scrub(CASES[case][0].encode())
    assert redaction.scrub(once) == (once, 0)


def test_bytes_that_are_not_text_pass_through_unchanged():
    given = b"\xff " + TOKEN.encode() + b"\n\xfe\n"
    assert redaction.scrub(given) == (b"\xff " + HIDDEN.encode() + b"\n\xfe\n", 1)


OPENED = "-----BEGIN RSA PRIVATE" + " KEY-----\nAAAA"
CLOSED = "BBBB\n-----END RSA PRIVATE" + " KEY-----"


@pytest.mark.parametrize(
    "given",
    [
        json.dumps({"t": OPENED}) + "\n" + json.dumps({"t": CLOSED}) + "\n",
        OPENED + "\n" + json.dumps({"t": CLOSED}) + "\n",
        json.dumps({"t": f"{OPENED}\n{CLOSED}\n{OPENED}"}) + "\n",
    ],
)
def test_a_key_opened_without_its_end_in_the_same_value_or_text_is_refused(given):
    with pytest.raises(redaction.Unredactable, match="^secret_unredactable$"):
        redaction.scrub(given.encode())


def test_a_key_opened_and_closed_in_one_value_is_redacted():
    given = json.dumps({"t": f"{OPENED}\n{CLOSED}"}) + "\n"
    assert redaction.scrub(given.encode()) == (compact({"t": "[REDACTED:private_key]"}).encode() + b"\n", 1)
