import json

from scripts.ci_mutation import stats_orphans
from scripts.ci_mutation.stats_dependencies import SHARED


def test_report_counts_shared_bytes_once_and_preserves_every_file(tmp_path, capsys):
    tmp_path = tmp_path / "cache"
    tmp_path.mkdir()
    values = {"inputs": "é", "dependencies": "abc", "orphan": "unused"}
    for name, value in values.items():
        (tmp_path / f"{name}{SHARED}").write_text(value)
    for name in ("first", "second"):
        (tmp_path / f"{name}.json").write_text(
            json.dumps({"inputs": "inputs", "dependencies": {"test_one": "dependencies", "test_two": "inputs"}})
        )
    (tmp_path / "unrelated.index").write_text("ignored")
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}

    stats_orphans.main([str(tmp_path)])

    assert json.loads(capsys.readouterr().out) == {
        "records": 2,
        "shared_files": 3,
        "shared_bytes": 11,
        "orphan_files": 1,
        "orphan_bytes": 6,
    }
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == before


def test_report_empty_cache(tmp_path, capsys):
    stats_orphans.main([str(tmp_path)])
    assert json.loads(capsys.readouterr().out) == {
        "records": 0,
        "shared_files": 0,
        "shared_bytes": 0,
        "orphan_files": 0,
        "orphan_bytes": 0,
    }
