"""Every message of the Handoff v2 structural check, word for word and in order, whatever else the check learns."""

import pytest

from scripts.handoff.check import Contract, problems

pytestmark = pytest.mark.unit

HEADINGS = "Intent, Done, Stopped at, Decisions and promises, Next, Read first"
FAULTY = (
    "# Handoff\n"
    "## Intent\nRefuse a malformed handoff.\n"
    "## Stopped at\nSeam two.\n"
    "## Done\n- Merged without a trace\n- Seen green, hypothesis until the run lands\n- Queued\n"
    "## Extra\nNot a heading.\n"
    "## Next\n\n"
    "## Next\n1. Run it.\n"
    "## Read first\nthe spec\n"
)


def test_each_fault_of_a_handoff_v2_document_is_named_word_for_word() -> None:
    assert problems(FAULTY, lambda address: True) == [
        "the first line must be # Handoff v2",
        f"## Extra is not one of the six headings: {HEADINGS}",
        "## Decisions and promises is missing",
        "## Next appears more than once",
        f"the headings are out of order; keep {HEADINGS}",
        "## Next is empty; write None when there is nothing to say",
        "the completion marker <!-- handoff complete --> must be the last line",
        "Read first lists its addresses as bullets, each starting with a GitHub issue or pull request link, "
        "ledger:, workspace:, recap:, inbox:, seat: or mission:",
        "Done bullet 1 carries no evidence; add it or label the bullet hypothesis",
        "Done bullet 3 carries no evidence; add it or label the bullet hypothesis",
    ]


def test_only_done_bullets_of_a_handoff_v2_need_evidence() -> None:
    text = (
        "# Handoff v2\n"
        "## Intent\n- Refuse a malformed handoff\n"
        "## Done\nNone\n"
        "## Stopped at\n- Seam two, nothing proven\n"
        "## Decisions and promises\n- Keep the cache out\n"
        "## Next\n- Run it\n"
        "## Read first\nNone\n"
        "<!-- handoff complete -->\n"
    )
    assert problems(text, lambda address: False) == []


def test_each_read_first_and_wording_fault_of_a_handoff_v2_document_is_named_word_for_word() -> None:
    key = "AKIA" + "IOSFODNN7EXAMPLE"
    text = (
        "# Handoff v2\n"
        "## Intent\nRefuse a malformed handoff.\n"
        "## Done\nNone\n"
        f"## Stopped at\nSeam two in hooks/context/recycle.py at line 142, beside {key}.\n"
        "## Decisions and promises\nNone\n"
        "## Next\n1. Run it.\n"
        "## Read first\n- the spec of the change\n- ledger:sw/tasks/t1 what the task asks\n"
        "<!-- handoff complete -->\n"
    )
    assert problems(text, lambda address: False) == [
        "Read first entry 1 does not start with an address (a GitHub issue or pull request link, ledger:, "
        "workspace:, recap:, inbox:, seat: or mission:)",
        "Read first entry 2: ledger:sw/tasks/t1 does not resolve",
        "credential value found (aws_access_key); refer to it by its variable name",
        "file path hooks/context/recycle.py; name the module, type or command instead",
        "line number line 142; name the function or behaviour instead",
    ]


def test_a_contract_of_more_than_ten_headings_counts_them_in_digits() -> None:
    names = tuple(f"Part {number}" for number in range(1, 12))
    wide = Contract("# Wide", names)
    body = "".join(f"## {name}\nNone\n" for name in names)
    text = f"# Wide\n{body}## Extra\nNone\n<!-- handoff complete -->\n"
    assert problems(text, lambda address: True, wide) == [f"## Extra is not one of the 11 headings: {', '.join(names)}"]
