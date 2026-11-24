import base64
import json
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from scripts.ci_lab import plant_proofs, plant_publish
from scripts.ci_lab.webhooks import PLANT_PREFIX

pytestmark = pytest.mark.unit
REPOSITORY = "https://github.com/acme/widgets"


def git(root: Path, *args: str, data: bytes | None = None, environ: Mapping[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], input=data, capture_output=True, check=True, timeout=10, env=environ
    )
    return result.stdout.decode().strip()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "Proof")
    git(root, "config", "user.email", "proof@example.invalid")
    (root / "sample.py").write_text("before = 'café'\n")
    (root / "sample.py").chmod(0o755)
    (root / "--extra\tmode.py").write_text("before\n")
    (root / "untouched").write_bytes(b"unchanged\x00bytes\r\n")
    catalog = root / "tests/ci_lab/plants"
    catalog.mkdir(parents=True)
    (catalog / "lint.json").write_text(
        json.dumps(
            {
                "gate": "lint",
                "reason": "I001",
                "edits": [
                    {"path": "sample.py", "before": "before", "after": "after"},
                    {"path": "--extra\tmode.py", "before": "before", "after": "after"},
                ],
            }
        )
    )
    git(root, "add", ".")
    git(root, "commit", "-qm", "Seed")
    return root


class GitHub:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[tuple[str, str, Mapping[str, Any] | None]] = []
        self.corrupt = ""

    def __call__(self, method: str, path: str, body: Mapping[str, Any] | None) -> Any:
        self.calls.append((method, path, body))
        assert path.startswith("repos/acme/widgets/git/")
        resource = path.removeprefix("repos/acme/widgets/git/")
        if method == "GET":
            assert resource.startswith("matching-refs/heads/diffcheck/lab-plant-")
            branch = "refs/" + resource.removeprefix("matching-refs/")
            listing = git(self.root, "for-each-ref", "--format=%(refname) %(objectname)", branch)
            return [{"ref": line.split()[0], "object": {"sha": line.split()[1]}} for line in listing.splitlines()]
        assert body is not None
        if resource == "blobs":
            assert method == "POST" and body["encoding"] == "base64"
            sha = git(self.root, "hash-object", "-w", "--stdin", data=base64.b64decode(body["content"]))
        elif resource == "trees":
            assert method == "POST"
            environ = {**os.environ, "GIT_INDEX_FILE": str(self.root / ".git" / "api-index")}
            git(self.root, "read-tree", body["base_tree"], environ=environ)
            for entry in body["tree"]:
                assert entry["type"] == "blob"
                git(
                    self.root,
                    "update-index",
                    "--cacheinfo",
                    f"{entry['mode']},{entry['sha']},{entry['path']}",
                    environ=environ,
                )
            sha = git(self.root, "write-tree", environ=environ)
        elif resource == "commits":
            assert method == "POST" and len(body["parents"]) == 1
            environ = dict(os.environ)
            for role in ("author", "committer"):
                identity = body[role]
                assert identity == {
                    "name": "CI lab plant",
                    "email": "ci-lab@example.invalid",
                    "date": "2000-01-01T00:00:00+00:00",
                }
                for field, key in (("NAME", "name"), ("EMAIL", "email"), ("DATE", "date")):
                    environ[f"GIT_{role.upper()}_{field}"] = identity[key]
            sha = git(
                self.root,
                "commit-tree",
                body["tree"],
                "-p",
                body["parents"][0],
                data=body["message"].encode(),
                environ=environ,
            )
        else:
            assert resource == "refs" and method == "POST" or resource.startswith("refs/heads/") and method == "PATCH"
            branch = body["ref"] if method == "POST" else resource
            if method == "PATCH":
                assert body["force"] is False
                git(self.root, "merge-base", "--is-ancestor", branch, body["sha"])
            git(self.root, "update-ref", branch, body["sha"])
            return {"object": {"sha": "0" * 40 if self.corrupt == "refs" else body["sha"]}}
        return {"sha": "0" * 40 if resource == self.corrupt else sha}


@pytest.mark.feature("lab-static-plant-proofs")
def test_rest_publication_preserves_exact_trees_and_commit_identity(root: Path) -> None:
    pair = plant_proofs.prepare(root, "lint", "HEAD")
    api = GitHub(root)
    (root / "unrelated.py").write_text("other = True\n")
    git(root, "add", "unrelated.py")
    git(root, "commit", "-qm", "Advance the checkout")
    original = git(root, "rev-parse", "HEAD")
    for head in (pair.green, pair.red):
        plant_publish.publish(root, pair, head, REPOSITORY, api)
        assert git(root, "rev-parse", f"{PLANT_PREFIX}lint-{pair.base}") == head
    assert git(root, "rev-parse", f"{pair.green}^{{tree}}") == git(root, "rev-parse", f"{pair.base}^{{tree}}")
    assert git(root, "rev-parse", f"{pair.red}^") == pair.green
    assert git(root, "show", f"{pair.red}:sample.py") == "after = 'café'"
    assert git(root, "ls-tree", pair.red, "--", "sample.py").startswith("100755 blob ")
    assert git(root, "rev-parse", "HEAD") == original
    assert git(root, "status", "--porcelain") == ""
    assert [body["message"] for _, path, body in api.calls if path.endswith("/commits") and body] == [
        "CI lab lint twin\n",
        "CI lab lint plant\n",
    ]


@pytest.mark.feature("lab-static-plant-proofs")
def test_rest_reruns_never_rewind_a_completed_plant(root: Path) -> None:
    pair = plant_proofs.prepare(root, "lint", "HEAD")
    api = GitHub(root)
    for head in (pair.green, pair.red):
        plant_publish.publish(root, pair, head, REPOSITORY, api)
    api.calls.clear()
    for head in (pair.green, pair.red):
        plant_publish.publish(root, pair, head, REPOSITORY, api)
    assert len(api.calls) == 2 and all(method == "GET" for method, _, _ in api.calls)
    assert git(root, "rev-parse", f"{PLANT_PREFIX}lint-{pair.base}") == pair.red


@pytest.mark.parametrize("resource", ["blobs", "trees", "commits", "refs"])
@pytest.mark.feature("lab-static-plant-proofs")
def test_rest_rejects_an_unexpected_object_identity_before_moving_the_ref(root: Path, resource: str) -> None:
    pair = plant_proofs.prepare(root, "lint", "HEAD")
    api = GitHub(root)
    plant_publish.publish(root, pair, pair.green, REPOSITORY, api)
    api.corrupt = resource
    with pytest.raises(ValueError, match="identity"):
        plant_publish.publish(root, pair, pair.red, REPOSITORY, api)
    assert git(root, "rev-parse", f"{PLANT_PREFIX}lint-{pair.base}") == (pair.red if resource == "refs" else pair.green)


@pytest.mark.parametrize("head", ["green", "red"])
@pytest.mark.feature("lab-static-plant-proofs")
def test_rest_rejects_a_divergent_ref_without_writing_objects(root: Path, head: str) -> None:
    pair = plant_proofs.prepare(root, "lint", "HEAD")
    api = GitHub(root)
    git(root, "update-ref", f"{PLANT_PREFIX}lint-{pair.base}", pair.base)
    with pytest.raises(ValueError, match="divergent"):
        plant_publish.publish(root, pair, getattr(pair, head), REPOSITORY, api)
    assert len(api.calls) == 1 and api.calls[0][0] == "GET"
