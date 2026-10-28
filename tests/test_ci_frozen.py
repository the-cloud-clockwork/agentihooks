import json
import subprocess

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
