import json

import pytest

from scripts.ci_mutation import merge


def _file(path, counts, failures=(), untouched=(), cleared=()):
    return {
        "path": path,
        "counts": counts,
        "failures": list(failures),
        "untouched_survivors": list(untouched),
        "cleared": list(cleared),
    }


def _write(folder, shard, report):
    path = folder / f"mutation-preflight-shard-{shard}" / "report.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(report))


def test_merged_report_sums_counts_and_keeps_every_shard_row_per_file():
    first = {
        "files": [
            _file("scripts/a.py", {"killed": 3, "survived": 1}, failures=[{"name": "a1"}]),
            _file("scripts/b.py", {}),
        ],
        "not_mutated": [],
        "failed": True,
    }
    second = {
        "files": [
            _file("scripts/a.py", {"killed": 2}, untouched=[{"name": "a2"}], cleared=[{"name": "a3"}]),
            _file("scripts/b.py", {}),
        ],
        "not_mutated": [{"path": "scripts/c.py", "reason": "over budget"}],
        "failed": True,
    }
    merged = merge.merge_reports([first, second])
    assert merged == {
        "files": [
            _file(
                "scripts/a.py",
                {"killed": 5, "survived": 1},
                failures=[{"name": "a1"}],
                untouched=[{"name": "a2"}],
                cleared=[{"name": "a3"}],
            ),
            _file("scripts/b.py", {}),
        ],
        "not_mutated": [{"path": "scripts/c.py", "reason": "over budget"}],
        "failed": True,
    }


def test_merged_report_is_green_only_when_every_shard_is_green():
    green = {"files": [_file("scripts/a.py", {"killed": 1})], "not_mutated": [], "failed": False}
    assert merge.merge_reports([green, green])["failed"] is False
    assert merge.merge_reports([green, {**green, "failed": True}])["failed"] is True


def test_not_mutated_paths_are_listed_once_with_the_first_reason():
    reports = [
        {"files": [], "not_mutated": [{"path": "scripts/a.py", "reason": "over budget"}], "failed": True},
        {"files": [], "not_mutated": [{"path": "scripts/a.py", "reason": "mutmut failed"}], "failed": True},
    ]
    assert merge.merge_reports(reports)["not_mutated"] == [{"path": "scripts/a.py", "reason": "over budget"}]


def test_main_writes_one_report_from_every_expected_shard(tmp_path, capsys):
    shards = tmp_path / "shards"
    for shard in (0, 1):
        _write(shards, shard, {"files": [_file("scripts/a.py", {"killed": 1})], "not_mutated": [], "failed": False})
    out = tmp_path / "gate"
    assert merge.main([str(shards), "--shards", "2", "--output", str(out)]) == 0
    report = json.loads((out / "report.json").read_text())
    assert report == {"files": [_file("scripts/a.py", {"killed": 2})], "not_mutated": [], "failed": False}
    assert "Merged 2 of 2 mutation shard reports" in capsys.readouterr().out


def test_main_fails_and_names_the_missing_shard_when_a_report_is_absent(tmp_path, capsys):
    shards = tmp_path / "shards"
    _write(shards, 1, {"files": [_file("scripts/a.py", {"killed": 1})], "not_mutated": [], "failed": False})
    out = tmp_path / "gate"
    assert merge.main([str(shards), "--shards", "3", "--output", str(out)]) == 1
    assert json.loads((out / "report.json").read_text())["failed"] is True
    assert "missing mutation shard reports: 0, 2" in capsys.readouterr().out


def test_main_fails_when_a_shard_report_failed(tmp_path):
    shards = tmp_path / "shards"
    _write(shards, 0, {"files": [], "not_mutated": [{"path": "scripts/a.py", "reason": "x"}], "failed": True})
    assert merge.main([str(shards), "--shards", "1", "--output", str(tmp_path / "gate")]) == 1


def test_not_mutated_paths_from_different_files_are_all_kept():
    reports = [
        {"files": [], "not_mutated": [{"path": "scripts/a.py", "reason": "over budget"}], "failed": True},
        {"files": [], "not_mutated": [{"path": "scripts/b.py", "reason": "over budget"}], "failed": True},
    ]
    assert [entry["path"] for entry in merge.merge_reports(reports)["not_mutated"]] == ["scripts/a.py", "scripts/b.py"]


@pytest.mark.parametrize("dropped", ["--shards", "--output"])
def test_main_refuses_to_merge_without_the_shard_count_or_the_output(tmp_path, dropped):
    argv = {"--shards": "1", "--output": str(tmp_path / "gate")}
    del argv[dropped]
    with pytest.raises(SystemExit) as refused:
        merge.main([str(tmp_path), *(item for pair in argv.items() for item in pair)])
    assert refused.value.code == 2


def test_main_writes_into_a_new_nested_folder_and_into_an_existing_one(tmp_path, capsys):
    shards = tmp_path / "shards"
    _write(shards, 1, {"files": [], "not_mutated": [], "failed": False})
    nested = tmp_path / "new" / "gate"
    assert merge.main([str(shards), "--shards", "3", "--output", str(nested)]) == 1
    assert merge.main([str(shards), "--shards", "3", "--output", str(nested)]) == 1
    assert json.loads((nested / "report.json").read_text())["failed"] is True
    assert capsys.readouterr().out.count("Merged 1 of 3 mutation shard reports") == 2


def test_main_writes_a_complete_empty_report_when_the_plan_found_nothing_to_mutate(tmp_path, capsys):
    out = tmp_path / "gate"
    assert merge.main([str(tmp_path / "absent"), "--shards", "0", "--output", str(out)]) == 0
    assert json.loads((out / "report.json").read_text()) == {"files": [], "not_mutated": [], "failed": False}
    assert capsys.readouterr().out == "Merged 0 of 0 mutation shard reports\n"
