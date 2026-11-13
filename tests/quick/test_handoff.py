from pathlib import Path

import pytest

from scripts.handoff import check
from scripts.quick import handoff, member, paths, registry

pytestmark = pytest.mark.unit

VALID = """# Handoff v2
## Intent
Ship the quick swarm tick.
## Done
- Built the layout, commit abc1234 pushed.
## Stopped at
Writing the tick round.
## Decisions and promises
None
## Next
Write the tick round; done when its test passes in CI.
## Read first
- seat:eng-1 what this seat already holds
<!-- handoff complete -->
"""


def _member(folder: Path) -> member.Member:
    seats = (
        registry.Seat(1, "master", "w1:p1", "claude", "quick-abc-master"),
        registry.Seat(2, "eng-1", "w1:p2", "claude", "quick-abc-eng-1"),
    )
    swarm = registry.Swarm("quick-abc", "/work", seats)
    registry.save(folder, swarm)
    paths.create(folder, ["master", "eng-1"])
    return member.Member(folder, swarm, seats[1])


def test_a_seat_address_is_a_read_first_address():
    assert check.ADDRESS.fullmatch("seat:eng-1") and check.ADDRESS.fullmatch("seat:master")
    assert "seat:" in check.ADDRESS_FORMS


def test_a_seat_address_resolves_only_for_a_seat_of_this_quick_swarm(tmp_path):
    _member(tmp_path)
    resolves = handoff.resolver(tmp_path)
    assert resolves("seat:eng-1") and resolves("seat:master")
    assert not resolves("seat:eng-7") and not resolves("seat:../eng-1") and not resolves("recap:x@y")
    assert not resolves("seat:.") and not resolves("seat:") and not resolves("seat:..")


@pytest.mark.feature("quick-swarm")
def test_an_accepted_handoff_is_kept_in_the_seat_folder_and_marks_the_seat_pending(tmp_path):
    found = _member(tmp_path)
    document = tmp_path / "draft.md"
    document.write_text(VALID)
    assert handoff.accept(found, document) == []
    assert paths.handoff(tmp_path, "eng-1").read_text() == VALID
    loaded = registry.load(tmp_path)
    assert loaded is not None and loaded.seat("eng-1") == registry.Seat(
        2, "eng-1", "w1:p2", "claude", "quick-abc-eng-1", state=registry.PENDING
    )


def test_the_seat_handoff_file_is_the_default_document(tmp_path):
    found = _member(tmp_path)
    paths.handoff(tmp_path, "eng-1").write_text(VALID)
    assert handoff.accept(found, None) == []


@pytest.mark.feature("quick-swarm")
def test_a_malformed_handoff_is_refused_and_the_seat_stays_ready(tmp_path):
    found = _member(tmp_path)
    document = tmp_path / "draft.md"
    document.write_text(VALID.replace("## Next", "## Later"))
    refused = handoff.accept(found, document)
    assert refused and any("## Next is missing" in problem for problem in refused)
    loaded = registry.load(tmp_path)
    seat = loaded.seat("eng-1") if loaded else None
    assert seat is not None and seat.state == registry.READY
    assert paths.handoff(tmp_path, "eng-1").read_text() == ""


def test_a_missing_document_is_refused(tmp_path):
    found = _member(tmp_path)
    assert handoff.accept(found, tmp_path / "absent.md") == [f"no handoff document at {tmp_path / 'absent.md'}"]
