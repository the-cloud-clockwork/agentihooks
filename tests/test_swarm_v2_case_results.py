import hashlib
import json
import subprocess

from scripts.swarm_v2 import case_results


def repo(tmp_path):
    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "fixture@example.com")
    git("config", "user.name", "fixture")
    (tmp_path / "input.py").write_text("first\n")
    (tmp_path / "out").mkdir()
    git("add", "input.py")
    git("commit", "-q", "-m", "fixture")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True).stdout.strip()
    return head


def test_each_case_result_and_the_manifest_name_the_commit_holding_the_inputs(tmp_path):
    head = repo(tmp_path)
    cases = (("a", lambda: {"passed": True, "then": "x"}), ("b", lambda: {"passed": False}))
    assert case_results.write(tmp_path, tmp_path / "out", ["input.py"], "fixture class", cases) == {
        "a": True,
        "b": False,
    }
    stamp = {"tested_commit": head, "evidence_class": "fixture class"}
    digest = hashlib.sha256(b"first\n").hexdigest()
    expected = {
        "a-result.json": {**stamp, "passed": True, "then": "x"},
        "b-result.json": {**stamp, "passed": False},
        "manifest.json": {**stamp, "inputs": {"input.py": digest}},
    }
    for name, body in expected.items():
        assert (tmp_path / "out" / name).read_text() == json.dumps(body, indent=2) + "\n"


def test_an_uncommitted_input_writes_nothing(tmp_path):
    repo(tmp_path)
    (tmp_path / "input.py").write_text("changed\n")
    ran = []
    assert case_results.write(tmp_path, tmp_path / "out", ["input.py"], "c", (("a", ran.append),)) is None
    assert ran == [] and list((tmp_path / "out").iterdir()) == []


def test_a_staged_but_uncommitted_input_writes_nothing(tmp_path):
    repo(tmp_path)
    (tmp_path / "input.py").write_text("staged\n")
    subprocess.run(["git", "add", "input.py"], cwd=tmp_path, check=True)
    assert case_results.write(tmp_path, tmp_path / "out", ["input.py"], "c", (("a", dict),)) is None


def test_an_input_named_like_a_revision_is_read_as_a_path(tmp_path):
    repo(tmp_path)
    (tmp_path / "HEAD").write_text("first\n")
    subprocess.run(["git", "add", "HEAD"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "named"], cwd=tmp_path, check=True)
    cases = (("a", lambda: {"passed": True}),)
    assert case_results.write(tmp_path, tmp_path / "out", ["HEAD"], "c", cases) == {"a": True}


def test_a_change_outside_the_inputs_does_not_stop_the_results(tmp_path):
    repo(tmp_path)
    (tmp_path / "other.py").write_text("tracked\n")
    subprocess.run(["git", "add", "other.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "other"], cwd=tmp_path, check=True)
    (tmp_path / "other.py").write_text("changed\n")
    cases = (("a", lambda: {"passed": True}),)
    assert case_results.write(tmp_path, tmp_path / "out", ["input.py"], "c", cases) == {"a": True}
