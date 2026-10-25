import json
from pathlib import Path

import pytest

from scripts.swarm_v2.herdr import deliveries
from scripts.swarm_v2.herdr.target import DELIVERIES

pytestmark = pytest.mark.unit
KEY = "exe-a/2/w1:p1"
JOURNAL = {
    "op-sent": {"target": KEY, "state": "sent"},
    "op-pending": {"target": KEY, "state": "pending", "since": 1000},
    "op-refused": {"target": KEY, "state": "refused", "reason": "herdr exited 1: busy"},
    "op-expired": {"target": KEY, "state": "expired", "since": 400},
}


def write(path: Path, entries: dict = JOURNAL) -> Path:
    path.write_text(json.dumps({"version": 1, "deliveries": entries}))
    return path


def test_the_listing_shows_pending_and_settled_prompts(tmp_path, capsys):
    assert deliveries.main(["--journal", str(write(tmp_path / DELIVERIES))], clock=lambda: 1599.9) == 0
    assert json.loads(capsys.readouterr().out) == {
        "pending": [{"operation_id": "op-pending", "target": KEY, "state": "pending", "since": 1000}],
        "settled": [
            {"operation_id": "op-sent", "target": KEY, "state": "sent"},
            {"operation_id": "op-refused", "target": KEY, "state": "refused", "reason": "herdr exited 1: busy"},
            {"operation_id": "op-expired", "target": KEY, "state": "expired", "since": 400},
        ],
    }


def test_a_prompt_pending_past_the_window_is_listed_as_expired(tmp_path, capsys):
    path = write(tmp_path / DELIVERIES, {"op-pending": JOURNAL["op-pending"]})
    assert deliveries.main(["--journal", str(path)], clock=lambda: 1600.0) == 0
    assert json.loads(capsys.readouterr().out) == {
        "pending": [],
        "settled": [{"operation_id": "op-pending", "target": KEY, "state": "expired", "since": 1000}],
    }
    assert json.loads(path.read_text())["deliveries"] == {"op-pending": JOURNAL["op-pending"]}


def test_the_listing_never_writes_beside_the_journal(tmp_path):
    path = write(tmp_path / DELIVERIES)
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    deliveries.main(["--journal", str(path)], clock=lambda: 1600.0)
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert [found.name for found in tmp_path.iterdir() if DELIVERIES in found.name] == [DELIVERIES]


def test_the_listing_reads_the_controller_journal_by_default(capsys):
    folder = Path.home() / ".ssh" / "swarm-terminals"
    folder.mkdir(parents=True)
    write(folder / DELIVERIES, {"op-pending": JOURNAL["op-pending"]})
    assert deliveries.journal() == folder / DELIVERIES
    assert deliveries.main([], clock=lambda: 1000.0) == 0
    assert [row["operation_id"] for row in json.loads(capsys.readouterr().out)["pending"]] == ["op-pending"]


def test_a_missing_journal_lists_nothing(tmp_path, capsys):
    assert deliveries.main(["--journal", str(tmp_path / DELIVERIES)]) == 0
    assert json.loads(capsys.readouterr().out) == {"pending": [], "settled": []}
    assert not (tmp_path / DELIVERIES).exists()


def test_an_unreadable_journal_is_refused(tmp_path, capsys):
    (tmp_path / DELIVERIES).write_text("{")
    assert deliveries.main(["--journal", str(tmp_path / DELIVERIES)]) == 1
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", "the pane delivery journal is unreadable\n")


def test_the_listing_is_indented_and_sorted(tmp_path, capsys):
    deliveries.main(["--journal", str(write(tmp_path / DELIVERIES, {"op-sent": JOURNAL["op-sent"]}))])
    expected = {"pending": [], "settled": [{"operation_id": "op-sent", "target": KEY, "state": "sent"}]}
    assert capsys.readouterr().out == json.dumps(expected, indent=2, sort_keys=True) + "\n"


def test_the_help_names_the_command_and_what_it_reads(capsys):
    with pytest.raises(SystemExit) as stopped:
        deliveries.main(["--help"])
    assert stopped.value.code == 0
    assert " ".join(capsys.readouterr().out.split()) == (
        "usage: python -m scripts.swarm_v2.herdr.deliveries [-h] [--journal JOURNAL] "
        "List the pending and settled remote prompts in a pane delivery journal; it reads only. "
        "options: -h, --help show this help message and exit "
        "--journal JOURNAL the journal file, by default this controller's own"
    )
