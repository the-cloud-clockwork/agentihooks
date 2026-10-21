import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from scripts.ci_mutation import replay


@pytest.mark.parametrize("changed_scope", [False, True])
def test_replay_preserves_sources_tests_and_selected_mutants(tmp_path, monkeypatch, capsys, changed_scope):
    archive = tmp_path / "input.zip"
    name = "scripts.sample.x_f__mutmut_1"
    scope = {"scripts/sample.py": {"lines": [2], "tests": ["tests/test_sample.py"]}}
    stats = [
        {
            "tests": {"scripts.sample.x_f": ["tests/test_sample.py::test_f"]},
            "durations": {"tests/test_sample.py::test_f": 100.0},
        }
    ]
    prefix = "archive/job/old"
    with ZipFile(archive, "w") as zipfile:
        zipfile.writestr("unrelated.py.meta", json.dumps({"exit_code_by_key": {"foreign": None}}))
        zipfile.writestr(f"{prefix}/scripts/", "")
        zipfile.writestr(f"{prefix}/data/deep/nested/extra.txt", "preserved")
        for path, content in {
            "changed-lines.json": json.dumps(scope),
            "shared-stats.json": json.dumps(stats),
            "scripts/sample.py": "def f():\n    return 1\n",
            "tests/test_sample.py": "from scripts.sample import f\n",
            "scripts/ci_mutation/old.py": "old = True\n",
            "mutants/scripts/sample.py.meta": json.dumps({"exit_code_by_key": {name: None}}),
            "mutants/marker.json": json.dumps({"exit_code_by_key": {"foreign": None}}),
            "killers/old.json": "{}",
        }.items():
            zipfile.writestr(f"{prefix}/{path}", content)
    downloaded = []

    def download(command, *, stdout, check):
        downloaded.append((command, check))
        stdout.write(archive.read_bytes())

    monkeypatch.setattr(replay.subprocess, "run", download)
    monkeypatch.setattr(
        "scripts.ci_mutation.measured.selected_mutant_names",
        lambda *args: ["x_other__mutmut_1" if changed_scope else "x_f__mutmut_1"],
    )
    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    folder = tmp_path / "nested/replay"
    cwd = Path.cwd()
    if changed_scope:
        with pytest.raises(ValueError) as error:
            replay.prepare("second", folder)
        assert str(error.value) == "replay plan changed the retained mutant scope"
    else:
        plan = replay.prepare("second", folder)
        assert plan["assignments"] == {name: 0}
        assert plan["head"] == "retained"
        assert plan["costs"][name] >= 100
        assert json.loads((folder / "stats/plan.json").read_text()) == plan
        assert json.loads((folder / "scope.json").read_text()) == scope
        assert (folder / "retained.zip").read_bytes() == archive.read_bytes()
        assert (folder / "source/scripts/ci_mutation/measured.py").read_text() == (
            Path(replay.__file__).parent / "measured.py"
        ).read_text()
        manifest = json.loads((folder / "manifest.json").read_text())
        assert manifest["case"] == "second"
        assert manifest["artifact"] == 11756407264
        assert manifest["mutants"] == 1
        assert set(manifest["source_test_sha256"]) == {"scripts/sample.py", "tests/test_sample.py"}
        assert (folder / "source/scripts/sample.py").read_text() == "def f():\n    return 1\n"
        assert not (folder / "source/mutants").exists()
        assert not (folder / "source/killers").exists()
        assert output.read_text() == "shards=[0]\n"
        assert json.loads(capsys.readouterr().out) == {**manifest, "shards": 1}
        monkeypatch.delenv("GITHUB_OUTPUT")
        monkeypatch.setattr("sys.argv", ["replay", "prepare", "--folder", str(folder)])
        assert replay.main() == 0
        assert json.loads(capsys.readouterr().out) == {**manifest, "shards": 1}
    assert Path.cwd() == cwd
    assert downloaded == [
        (["gh", "api", "repos/the-cloud-clockwork/agentihooks/actions/artifacts/11756407264/zip"], True)
    ] * (1 if changed_scope else 2)


@pytest.mark.parametrize(
    "fault", ["", "missing", "budget", "ungraded", "wrong count", "all killed", "cleared", "timeout only", "no tests"]
)
def test_replay_accepts_complete_survivor_failures_and_rejects_incomplete_grades(tmp_path, monkeypatch, capsys, fault):
    (tmp_path / "stats").mkdir()
    (tmp_path / "grades/shard-0").mkdir(parents=True)
    (tmp_path / "manifest.json").write_text(json.dumps({"case": "third", "mutants": 1}))
    (tmp_path / "stats/plan.json").write_text(json.dumps({"shards": 1, "assignments": {"survivor": 0}}))
    survivor = {"name": "survivor", "status": "survived"}
    file = {
        "path": "scripts/sample.py",
        "counts": {"survived": 1},
        "failures": [survivor],
        "cleared": [],
        "untouched_survivors": [],
    }
    if fault == "no tests":
        survivor["status"] = "no tests"
    if fault == "all killed":
        file.update(counts={"killed": 1}, failures=[])
    elif fault == "cleared":
        file.update(failures=[], cleared=[survivor])
    elif fault == "timeout only":
        file.update(counts={"timeout": 1}, failures=[{"name": "timeout", "status": "timeout"}])
    report = {
        "files": [file],
        "not_mutated": [],
        "failed": fault not in {"all killed", "cleared"},
        "shard": {
            "index": 0,
            "expected": 2 if fault == "wrong count" else 1,
            "graded": 0 if fault == "ungraded" else 1,
            "complete": fault != "ungraded",
            "budget_exhausted": fault == "budget",
        },
    }
    if fault != "missing":
        (tmp_path / "grades/shard-0/report.json").write_text(json.dumps(report))
    monkeypatch.setattr("sys.argv", ["replay", "verify", "--folder", str(tmp_path)])
    assert replay.main() == (0 if fault in {"", "no tests"} else 1)
    proof = json.loads((tmp_path / "replay-proof.json").read_text())
    assert json.loads(capsys.readouterr().out) == proof
    assert proof["files"] == ([] if fault == "missing" else [file])
    assert proof["shards"] == ([] if fault == "missing" else [report["shard"]])
    assert proof["survivor_failures"] == (0 if fault in {"missing", "all killed", "cleared", "timeout only"} else 1)
    assert proof["case"] == "third"
    assert proof["expected"] == 1
    assert proof["graded"] == (0 if fault in {"missing", "ungraded"} else 1)
    assert proof["exhausted_shards"] == (1 if fault == "budget" else 0)
    assert proof["survivor_grade_failed"] is (fault not in {"missing", "all killed", "cleared", "timeout only"})
    assert proof["complete"] is (fault not in {"missing", "ungraded", "wrong count"})


@pytest.mark.parametrize(
    "status,reason,expected",
    [
        ("killed", "", 0),
        ("survived", "", 1),
        ("killed", "over budget", 1),
        ("missing grade", "", 1),
        ("cleared", "", 0),
    ],
)
@pytest.mark.parametrize("explicit", [False, True])
def test_replay_grade_keeps_the_normal_mutation_failure_exit(
    tmp_path, monkeypatch, capsys, status, reason, expected, explicit
):
    source = tmp_path / "source"
    source.mkdir()
    stats = tmp_path / "stats"
    stats.mkdir()
    name = "scripts.sample.x_f__mutmut_1"
    index, total = (1, 3) if explicit else (0, 1)
    (stats / "plan.json").write_text(json.dumps({"assignments": {name: index}, "costs": {name: 10}}))
    (tmp_path / "scope.json").write_text(
        json.dumps({"scripts/sample.py": {"lines": [2], "tests": ["tests/test_sample.py"]}})
    )
    row = {
        "name": name,
        "status": "survived" if status == "cleared" else status,
        "lines": [2],
        "fingerprint": "abc",
        "diff": "changed",
    }
    if status == "cleared":
        (source / "mutation-cleared.txt").write_text(
            json.dumps({f"scripts/sample.py:{name}:abc": {"reader": "Standards", "reason": "Equivalent fixture"}})
        )
    calls = []

    def mutate(root, work, selected, deadline, shard, shared):
        calls.append((root, work, selected, shard, shared))
        assert deadline == 1180
        return {"scripts/sample.py": [] if status == "missing grade" else [row]}, reason

    monkeypatch.setattr(replay, "mutate_files", mutate)
    monkeypatch.setattr(replay.time, "monotonic", lambda: 100)
    arguments = ["--shard", "1", "--shards", "3"] if explicit else []
    monkeypatch.setattr("sys.argv", ["replay", "grade", "--folder", str(tmp_path), *arguments])
    assert replay.main() == expected
    report = json.loads((tmp_path / f"grade-{index}/report.json").read_text())
    assert json.loads(capsys.readouterr().out.splitlines()[-1]) == report
    assert report["failed"] is (bool(reason) or status == "survived")
    assert report["not_mutated"] == ([{"path": "scripts/sample.py", "reason": reason}] if reason else [])
    if not reason:
        assert report["files"][0]["path"] == "scripts/sample.py"
    assert report["shard"]["budget_seconds"] == 1080
    assert report["shard"]["complete"] is (not bool(reason) and status != "missing grade")
    assert report["shard"]["graded"] == (0 if reason or status == "missing grade" else 1)
    assert len(calls) == 1
    assert calls[0][0] == source
    assert calls[0][1] == tmp_path / f"grade-{index}/work"
    assert calls[0][2] == {"scripts/sample.py": ({2}, ["tests/test_sample.py"])}
    assert calls[0][3] == (index, total)
    assert calls[0][4].folder == stats / "group-0"
    assert calls[0][4].head == "retained"


@pytest.mark.parametrize("args", [["invalid"], ["prepare", "--case", "invalid"]])
def test_replay_cli_rejects_unknown_modes_and_cases(monkeypatch, args):
    monkeypatch.setattr("sys.argv", ["replay", *args])
    with pytest.raises(SystemExit) as error:
        replay.main()
    assert error.value.code == 2


def test_replay_cli_defaults_to_its_work_folder_and_accepts_the_third_case(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls = []
    monkeypatch.setattr(replay, "prepare", lambda case, folder: calls.append((case, folder)))
    monkeypatch.setattr("sys.argv", ["replay", "prepare", "--case", "third"])
    assert replay.main() == 0
    assert calls == [("third", tmp_path / ".mutation-replay")]


@pytest.mark.parametrize("cleared", [False, True])
def test_replay_verify_counts_every_unit_and_grades_split_survivors_against_the_source_clearances(
    tmp_path, monkeypatch, capsys, cleared
):
    from scripts.ci_mutation.clearances import write_clearance

    (tmp_path / "manifest.json").write_text(json.dumps({"case": "third", "mutants": 1}))
    (tmp_path / "stats").mkdir()
    (tmp_path / "stats/plan.json").write_text(json.dumps({"shards": 2, "assignments": {"split": [1, 0]}}))
    (tmp_path / "source").mkdir()
    if cleared:
        write_clearance(tmp_path / "source", "scripts/sample.py:split:f", {"reader": "Standards", "reason": "same"})
    row = {"name": "split", "status": "survived", "lines": [2], "fingerprint": "f", "diff": "changed"}
    for index in range(2):
        shard = {"index": index, "expected": 1, "graded": 1, "complete": True, "budget_exhausted": False}
        report = {
            "files": [],
            "not_mutated": [],
            "failed": False,
            "units": [{"path": "scripts/sample.py", "lines": [2], "rows": [row]}],
            "shard": shard,
        }
        (tmp_path / f"grades/shard-{index}").mkdir(parents=True)
        (tmp_path / f"grades/shard-{index}/report.json").write_text(json.dumps(report))
    monkeypatch.setattr("sys.argv", ["replay", "verify", "--folder", str(tmp_path)])
    assert replay.main() == (1 if cleared else 0)
    proof = json.loads((tmp_path / "replay-proof.json").read_text())
    assert (proof["complete"], proof["expected"], proof["graded"]) == (True, 2, 2)
    assert proof["survivor_failures"] == (0 if cleared else 1)
    assert [entry["name"] for entry in proof["files"][0]["cleared"]] == (["split"] if cleared else [])


def test_replay_grade_hands_split_units_to_the_merge_instead_of_grading_them(tmp_path, monkeypatch, capsys):
    (tmp_path / "source").mkdir()
    stats = tmp_path / "stats"
    stats.mkdir()
    name = "scripts.sample.x_f__mutmut_1"
    split = {"head": ["h"], "slices": [["a"], ["b"]], "costs": [3, 4]}
    plan = {"assignments": {name: [0, 1]}, "costs": {name: 7}, "splits": {"scripts.sample.x_f": split}}
    (stats / "plan.json").write_text(json.dumps(plan))
    (tmp_path / "scope.json").write_text(
        json.dumps({"scripts/sample.py": {"lines": [2], "tests": ["tests/test_sample.py"]}})
    )
    row = {"name": name, "status": "survived", "lines": [2], "fingerprint": "abc", "diff": "changed"}
    monkeypatch.setattr(replay, "mutate_files", lambda *args: ({"scripts/sample.py": [row]}, ""))
    monkeypatch.setattr("sys.argv", ["replay", "grade", "--folder", str(tmp_path)])
    assert replay.main() == 0
    report = json.loads((tmp_path / "grade-0/report.json").read_text())
    assert report["files"][0]["counts"] == {}
    assert report["units"] == [{"path": "scripts/sample.py", "lines": [2], "rows": [row]}]
    assert report["failed"] is False
