import json
from pathlib import Path

from scripts.ci_mutation.stats import SharedStats, load_parts, stats_key, write_part

SELECTED = {"hooks/a.py": ({3, 1}, ["tests/test_b.py", "tests/test_a.py"]), "hooks/b.py": (set(), [])}


def test_key_covers_the_head_and_every_selected_path_line_and_test():
    key = stats_key("abc", SELECTED)
    assert len(key) == 64
    assert key == stats_key(
        "abc", {"hooks/b.py": (set(), []), "hooks/a.py": ({1, 3}, ["tests/test_a.py", "tests/test_b.py"])}
    )
    assert key != stats_key("abd", SELECTED)
    assert key != stats_key("abc", {**SELECTED, "hooks/a.py": ({1}, ["tests/test_a.py", "tests/test_b.py"])})
    assert key != stats_key("abc", {**SELECTED, "hooks/a.py": ({1, 3}, ["tests/test_a.py"])})
    assert key != stats_key("abc", {"hooks/a.py": SELECTED["hooks/a.py"]})


def test_group_folder_keeps_the_head_and_part():
    stats = SharedStats(Path("/stats"), "abc", (1, 2))
    assert stats.group(3) == SharedStats(Path("/stats/group-3"), "abc", (1, 2))
    assert SharedStats(Path("/stats"), "abc").part is None


def test_parts_round_trip_in_part_order(tmp_path):
    first, second = (
        [{"tests": {"f": ["t1"]}, "durations": {"t1": 1}, "cpu": 2}],
        [{"tests": {}, "durations": {}, "cpu": 0}],
    )
    write_part(tmp_path / "group-0/part-1.json", "k", (1, 2), second)
    write_part(tmp_path / "group-0/part-0.json", "k", (0, 2), first)
    assert json.loads((tmp_path / "group-0/part-1.json").read_text()) == {
        "key": "k",
        "part": 1,
        "parts": 2,
        "results": second,
    }
    assert load_parts(tmp_path / "group-0", "k") == (first + second, "")


def test_an_empty_part_still_counts(tmp_path):
    write_part(tmp_path / "part-0.json", "k", (0, 1), [])
    assert load_parts(tmp_path, "k") == ([], "")


def test_missing_folder_or_parts_name_what_is_missing(tmp_path):
    assert load_parts(tmp_path / "absent", "k") == (
        [],
        f"mutation stats missing: no stats part in {tmp_path / 'absent'}",
    )
    (tmp_path / "empty").mkdir()
    assert load_parts(tmp_path / "empty", "k")[1].startswith("mutation stats missing: no stats part")
    write_part(tmp_path / "part-0.json", "k", (0, 3), [])
    write_part(tmp_path / "part-2.json", "k", (2, 3), [])
    assert load_parts(tmp_path, "k") == ([], "mutation stats missing: found parts [0, 2] of [3]")


def test_parts_that_disagree_on_their_count_are_missing(tmp_path):
    write_part(tmp_path / "part-0.json", "k", (0, 2), [])
    write_part(tmp_path / "part-1.json", "k", (1, 3), [])
    assert load_parts(tmp_path, "k") == ([], "mutation stats missing: found parts [0, 1] of [2, 3]")


def test_a_part_collected_for_another_commit_or_selection_is_stale(tmp_path):
    write_part(tmp_path / "part-0.json", "k", (0, 3), [{"tests": {}, "durations": {}, "cpu": 0}])
    write_part(tmp_path / "part-1.json", "old", (1, 3), [])
    write_part(tmp_path / "part-2.json", "old", (2, 3), [])
    assert load_parts(tmp_path, "k") == (
        [],
        "mutation stats stale: parts [1, 2] were collected for another commit or selection",
    )
