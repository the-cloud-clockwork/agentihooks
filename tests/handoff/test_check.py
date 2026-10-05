import pytest

from scripts.handoff.check import MARKER, problems

pytestmark = pytest.mark.unit

VALID = """# Handoff v2
## Intent
Refuse a malformed handoff, as part of the Handoff v2 phase.
## Done
- Checker module merged in https://github.com/o/r/pull/12
- Seam one test went red then green with `pytest -k refusal`, 9 passed
- The gate probably needs no cache, hypothesis until the timing run
## Stopped at
Seam two test written, not yet run.
## Decisions and promises
None
## Next
1. Run the seam two test; done when it is red for the expected reason.
2. Make it green.
## Read first
- https://github.com/o/r/issues/11 the spec and its seams
- ledger:sw/tasks/t1 the task row and its proof contract
- workspace:t1/progress what landed so far
- recap:eng-1@sw what the last occupant did
- inbox:ab12cd34 the question the master asked
<!-- handoff complete -->
"""


def _swap(old, new, text=VALID):
    assert old in text
    return text.replace(old, new, 1)


def test_a_conforming_document_has_no_problems():
    assert problems(VALID, lambda address: True) == []


def test_trailing_blank_lines_after_the_marker_are_fine():
    assert problems(VALID + "\n\n", lambda address: True) == []


def test_missing_title_is_refused():
    assert any("# Handoff v2" in p for p in problems(_swap("# Handoff v2\n", ""), lambda a: True))


def test_missing_heading_is_refused():
    found = problems(_swap("## Stopped at\nSeam two test written, not yet run.\n", ""), lambda a: True)
    assert any("Stopped at" in p and "missing" in p for p in found)


def test_duplicate_heading_is_refused():
    found = problems(_swap("## Next\n", "## Next\nfirst\n## Next\n"), lambda a: True)
    assert any("Next" in p and "more than once" in p for p in found)


def test_headings_out_of_order_are_refused():
    text = _swap("## Intent\nRefuse a malformed handoff, as part of the Handoff v2 phase.\n", "")
    text = _swap("## Next\n", "## Intent\nRefuse a malformed handoff.\n## Next\n", text)
    assert any("order" in p for p in problems(text, lambda a: True))


def test_an_unknown_section_heading_is_refused():
    found = problems(_swap("## Next\n", "## Notes\nsomething\n## Next\n"), lambda a: True)
    assert any("Notes" in p for p in found)


def test_a_heading_inside_a_code_fence_is_not_a_heading():
    text = _swap("Seam two test written, not yet run.\n", "Seam two test written.\n```\n## Next\n```\n")
    assert problems(text, lambda a: True) == []


def test_an_empty_section_must_say_none():
    found = problems(_swap("## Decisions and promises\nNone\n", "## Decisions and promises\n\n"), lambda a: True)
    assert any("Decisions and promises" in p and "None" in p for p in found)


def test_the_marker_must_be_the_last_line():
    found = problems(VALID.replace(MARKER + "\n", MARKER + "\nlate words\n"), lambda a: True)
    assert any("last line" in p for p in found)


def test_a_missing_marker_is_refused():
    assert any("last line" in p for p in problems(VALID.replace(MARKER, ""), lambda a: True))


def test_every_read_first_address_must_resolve():
    found = problems(VALID, lambda address: address != "ledger:sw/tasks/t1")
    assert found == ["Read first entry 2: ledger:sw/tasks/t1 does not resolve"]


def test_a_read_first_entry_without_an_address_is_refused():
    found = problems(_swap("- recap:eng-1@sw", "- the recap of the last occupant"), lambda a: True)
    assert any("Read first entry 4" in p and "address" in p for p in found)


@pytest.mark.parametrize(
    "secret",
    ["ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8", "AKIA" + "IOSFODNN7EXAMPLE"],
)
def test_a_credential_value_is_refused(secret):
    found = problems(_swap("Seam two test written", f"Seam two test written with {secret}"), lambda a: True)
    assert any("credential" in p for p in found)


@pytest.mark.parametrize(
    "path",
    ["hooks/context/context_recycle.py", "/home/u/dev/repo", "~/scratchpad/notes", "context_recycle.py", "./run.sh"],
)
def test_a_file_path_is_refused(path):
    found = problems(_swap("Seam two test written", f"Seam two test written in {path}"), lambda a: True)
    assert any("file path" in p and path in p for p in found)


@pytest.mark.parametrize("where", ["gate.py:142", "line 142", "lines 10 to 20", "L142"])
def test_a_line_number_is_refused(where):
    found = problems(_swap("Seam two test written", f"Seam two test written at {where}"), lambda a: True)
    assert any("line number" in p for p in found)


def test_words_with_slashes_and_times_are_not_paths_or_lines():
    text = _swap("Seam two test written", "Seam two pass/fail test written at 10:30 UTC for eng/ci, version 2.3")
    assert problems(text, lambda a: True) == []


def test_a_done_bullet_without_evidence_must_say_hypothesis():
    found = problems(
        _swap("- Checker module merged in https://github.com/o/r/pull/12", "- Checker merged"), lambda a: 1
    )
    assert found == ["Done bullet 1 carries no evidence; add it or label the bullet hypothesis"]


@pytest.mark.parametrize(
    "evidence",
    ["commit 3f9c2ab", "PR #12", "run 18234567890", "`ruff check` clean", "proof in workspace:t1/proof"],
)
def test_each_kind_of_evidence_counts(evidence):
    assert problems(_swap("Checker module merged in https://github.com/o/r/pull/12", f"Merged, {evidence}"), bool) == []


def test_every_problem_is_reported_at_once():
    text = _swap("## Decisions and promises\nNone\n", "## Decisions and promises\n")
    text = text.replace(MARKER, "")
    assert len(problems(text, lambda a: True)) == 2
