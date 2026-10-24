import json

import pytest

from tests import shard_check

pytestmark = pytest.mark.unit


def _grade(tmp_path, monkeypatch, manifest, shards):
    collected = tmp_path / "collected.json"
    if manifest is not None:
        collected.write_text(manifest)
    paths = []
    for index, nodes in enumerate(shards):
        folder = tmp_path / str(index)
        folder.mkdir()
        path = folder / "durations.json"
        path.write_text(json.dumps(dict.fromkeys(nodes, 0.1)))
        (folder / "durations.sha256").write_text("a" * 64)
        paths.append(str(path))

    def refuse_collection(_):
        pytest.fail("Manifest grading must never collect executable tests")

    monkeypatch.setattr(shard_check, "collect", refuse_collection)
    return shard_check.main(["--collected", str(collected), *paths])


def test_manifest_grades_head_only_tests_without_collecting(tmp_path, monkeypatch, capsys):
    nodes = ["tests/test_head_only.py::test_new", "tests/test_old.py::test_existing"]
    assert _grade(tmp_path, monkeypatch, json.dumps(nodes), [[nodes[0]], [nodes[1]]]) == 0
    assert "2 collected tests, 0 ran zero times or more than once" in capsys.readouterr().out


@pytest.mark.parametrize(
    "shards,count",
    [
        ([[], ["test_head::test_new"]], 0),
        ([["test_head::test_old"], ["test_head::test_old", "test_head::test_new"]], 2),
    ],
)
def test_manifest_names_missing_or_duplicate_ownership(tmp_path, monkeypatch, capsys, shards, count):
    nodes = ["test_head::test_old", "test_head::test_new"]
    assert _grade(tmp_path, monkeypatch, json.dumps(nodes), shards) == 1
    assert f"test_head::test_old ran {count} times" in capsys.readouterr().out


@pytest.mark.parametrize(
    "manifest", [None, "{", "[]", "{}", '"test_head::test_new"', "[1]", '[""]', '["test_new", "test_new"]']
)
def test_invalid_or_missing_manifest_cannot_grade(tmp_path, monkeypatch, capsys, manifest):
    assert _grade(tmp_path, monkeypatch, manifest, [["test_head::test_new"]]) == 1
    assert "::error::" in capsys.readouterr().out
