import json
import subprocess

import pytest

from scripts import gate_protected

WORKFLOW = ".github/workflows/gate-protected.yml"
CONTENT = "name: Reviewed café\r\non: push\r\n".encode()


def git(root, *args, data=None):
    return subprocess.run(["git", "-C", str(root), *args], input=data, check=True, capture_output=True).stdout.strip()


def commit(root):
    git(root, "add", ".")
    git(root, "-c", "user.name=reader", "-c", "user.email=reader@example.org", "commit", "-qm", "record")
    return git(root, "rev-parse", "HEAD").decode()


def frozen_repo(tmp_path):
    root = tmp_path / "predecessor"
    root.mkdir()
    git(root, "init", "-q", "--initial-branch=dev")
    workflow = root / WORKFLOW
    workflow.parent.mkdir(parents=True)
    workflow.write_bytes(b"name: Old\n")
    registry = root / gate_protected.FROZEN
    registry.parent.mkdir(parents=True)
    registry.write_text(json.dumps({WORKFLOW: [git(root, "hash-object", WORKFLOW).decode()]}))
    commit(root)
    return root


def test_successor_lands_stored_candidate_without_another_announcement(tmp_path):
    from scripts import ci_frozen

    root = frozen_repo(tmp_path)
    candidate = tmp_path / "reviewed.yml"
    candidate.write_bytes(CONTENT)
    blob = ci_frozen.announce(root, WORKFLOW, candidate)
    assert (root / WORKFLOW).read_bytes() == b"name: Old\n"
    announcement = commit(root)
    assert gate_protected._frozen_problems(root, f"{announcement}^", f"{announcement}^", announcement) == []
    candidate.unlink()
    successor = tmp_path / "successor"
    git(tmp_path, "clone", "-q", "--no-local", str(root), str(successor))
    registry = (successor / gate_protected.FROZEN).read_bytes()

    ci_frozen.land(successor, WORKFLOW, blob, "origin/dev")

    assert (successor / WORKFLOW).read_bytes() == CONTENT
    assert (successor / gate_protected.FROZEN).read_bytes() == registry
    assert git(successor, "diff", "--name-only").decode() == WORKFLOW
    assert git(successor, "ls-files", "--others", "--exclude-standard") == b""
    landing = commit(successor)
    assert gate_protected._frozen_problems(successor, announcement, announcement, landing) == []


@pytest.mark.parametrize("fault", ["uncommitted", "unaccepted", "missing", "corrupted"])
def test_landing_refuses_invalid_stored_candidates(tmp_path, fault):
    from scripts import ci_frozen

    root = frozen_repo(tmp_path)
    candidate = tmp_path / "reviewed.yml"
    candidate.write_bytes(CONTENT)
    blob = ci_frozen.announce(root, WORKFLOW, candidate)
    stored = root / ci_frozen.CANDIDATES / blob
    if fault == "missing":
        stored.unlink()
    elif fault == "corrupted":
        stored.write_bytes(b"name: Unreviewed\n")
    if fault != "uncommitted":
        commit(root)
    if fault == "unaccepted":
        blob = git(root, "hash-object", "--stdin", data=b"unannounced").decode()
    before = (root / WORKFLOW).read_bytes()
    with pytest.raises((ValueError, subprocess.CalledProcessError)):
        ci_frozen.land(root, WORKFLOW, blob, "dev")
    assert (root / WORKFLOW).read_bytes() == before


@pytest.mark.parametrize("workflow", [".github/workflows/unknown.yml", "../escape.yml", "scripts/gate.py"])
def test_announcement_refuses_paths_outside_frozen_workflows(tmp_path, workflow):
    from scripts import ci_frozen

    root = frozen_repo(tmp_path)
    registry = root / gate_protected.FROZEN
    accepted = json.loads(registry.read_bytes())
    if workflow != ".github/workflows/unknown.yml":
        accepted[workflow] = []
        registry.write_text(json.dumps(accepted))
    before = registry.read_bytes()
    candidate = tmp_path / "reviewed.yml"
    candidate.write_bytes(CONTENT)
    with pytest.raises(ValueError, match="Not a frozen workflow"):
        ci_frozen.announce(root, workflow, candidate)
    assert registry.read_bytes() == before
    assert not (root / ci_frozen.CANDIDATES).exists()


def test_cli_reuses_an_announcement_and_lands_from_committed_bytes(tmp_path, monkeypatch, capsys):
    from scripts import ci_frozen

    root = frozen_repo(tmp_path)
    candidate = tmp_path / "reviewed.yml"
    candidate.write_bytes(CONTENT)
    monkeypatch.chdir(root)
    assert ci_frozen.main(["announce", WORKFLOW, str(candidate)]) == 0
    blob = capsys.readouterr().out.strip()
    assert blob == git(root, "hash-object", "--stdin", data=CONTENT).decode()
    assert ci_frozen.main(["announce", WORKFLOW, str(candidate)]) == 0
    assert capsys.readouterr().out.strip() == blob
    assert json.loads((root / gate_protected.FROZEN).read_bytes())[WORKFLOW].count(blob) == 1
    announcement = commit(root)
    (root / ci_frozen.CANDIDATES / blob).write_bytes(b"uncommitted replacement")
    assert ci_frozen.main(["land", WORKFLOW, blob, "--base", "dev"]) == 0
    assert capsys.readouterr().out == ""
    assert (root / WORKFLOW).read_bytes() == CONTENT
    landing = commit(root)
    assert gate_protected._frozen_problems(root, f"{announcement}^", f"{announcement}^", landing)
