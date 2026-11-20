import base64
import os
import stat
import subprocess
from collections import OrderedDict
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from scripts.ci_lab import worktrees
from scripts.dep_manifest import _git

WRITE = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH


def _commit(repo: Path, name: str, text: str) -> str:
    (repo / name).write_text(text)
    _git(repo, "add", name)
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", name)
    return _git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    repo = tmp_path / "origin"
    _git(tmp_path, "init", "-q", "-b", "main", str(repo))
    return repo


def _mirror(tmp_path: Path, origin: Path, have: str) -> worktrees.Mirror:
    node = tmp_path / "node"
    _git(tmp_path, "init", "-q", "--bare", str(node))
    _git(node, "fetch", "-q", f"file://{origin}", f"{have}:refs/heads/main")
    return worktrees.Mirror(node=node, store=tmp_path / "store", remote=f"file://{origin}")


def _own_objects(store: Path) -> set[str]:
    return {f"{path.parent.name}{path.name}" for path in (store / "objects").glob("??/*")}


def _registered(store: Path) -> list[str]:
    listed = _git(store, "worktree", "list", "--porcelain").splitlines()
    paths = (line.removeprefix("worktree ") for line in listed if line.startswith("worktree "))
    return sorted(path for path in paths if path != str(store))


def _writable(tree: Path) -> list[str]:
    paths = (tree, *tree.rglob("*"))
    return sorted(str(path.relative_to(tree)) for path in paths if path.lstat().st_mode & WRITE)


def test_fetch_takes_nothing_when_the_node_mirror_holds_the_sha(tmp_path: Path, origin: Path) -> None:
    head = _commit(origin, "a", "a")
    mirror = _mirror(tmp_path, origin, head)
    assert mirror.fetch(head) is False
    assert (mirror.store / "objects/info/alternates").read_text() == f"{tmp_path / 'node' / 'objects'}\n"
    assert _own_objects(mirror.store) == set()
    assert _git(mirror.store, "cat-file", "-t", head) == "commit\n"


def test_fetch_takes_only_the_objects_the_node_mirror_lacks(
    tmp_path: Path, origin: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    base = _commit(origin, "a", "a")
    head = _commit(origin, "b", "b")
    mirror = _mirror(tmp_path, origin, base)
    missing = {line.split()[0] for line in _git(origin, "rev-list", "--objects", head, f"^{base}").splitlines()}
    assert mirror.fetch(head) is True
    assert len(missing) == 3
    assert _own_objects(mirror.store) == missing
    assert mirror.fetch(head) is False
    assert _own_objects(mirror.store) == missing
    assert not (mirror.store / "FETCH_HEAD").exists()
    assert capfd.readouterr() == ("", "")


@pytest.mark.feature("lab-store-incremental-fetch")
@pytest.mark.parametrize("fallback", [False, True])
def test_fetch_without_a_node_mirror_keeps_heads_and_takes_only_new_objects(
    tmp_path: Path, origin: Path, fallback: bool
) -> None:
    base = _commit(origin, "a", "\n".join(str(number) for number in range(10000)))
    store = tmp_path / "store"
    _git(tmp_path, "init", "-q", "--bare", str(store))
    _git(store, "config", "fetch.unpackLimit", "1")
    remote = f"file://{origin}"
    mirror = worktrees.Mirror(
        node=tmp_path / "absent",
        store=store,
        remote=f"file://{tmp_path / 'unreachable'}" if fallback else remote,
        lab=worktrees.Lab(remote, "") if fallback else None,
        repository=remote,
    )
    assert mirror.fetch(base) is True
    before = set((store / "objects/pack").glob("*.idx"))
    head = _commit(origin, "b", "b")
    assert mirror.fetch(head) is True
    added = set((store / "objects/pack").glob("*.idx")) - before
    assert len(added) == 1
    packed = _git(store, "verify-pack", "-v", str(added.pop()))
    assert f"{head} commit" in packed
    assert f"{base} commit" not in packed
    assert set(_git(store, "for-each-ref", "--format=%(objectname)", "refs/lab/").splitlines()) == {base, head}
    assert mirror.fetch(base) is False
    assert mirror.fetch(head) is False
    assert not (store / "FETCH_HEAD").exists()


@pytest.mark.feature("lab-store-incremental-fetch")
@pytest.mark.parametrize("node_exists", [False, True])
def test_fetch_never_names_a_missing_node_objects_directory_in_alternates(
    tmp_path: Path, origin: Path, node_exists: bool, capfd: pytest.CaptureFixture[str]
) -> None:
    head = _commit(origin, "a", "a")
    node = tmp_path / "node"
    if node_exists:
        node.mkdir()
    mirror = worktrees.Mirror(node=node, store=tmp_path / "store", remote=f"file://{origin}")
    assert mirror.fetch(head) is True
    assert not (mirror.store / "objects/info/alternates").exists()
    assert _git(mirror.store, "cat-file", "-t", head) == "commit\n"
    assert "alternate" not in capfd.readouterr().err


def test_fetch_of_an_unknown_sha_raises(tmp_path: Path, origin: Path) -> None:
    mirror = _mirror(tmp_path, origin, _commit(origin, "a", "a"))
    with pytest.raises(subprocess.CalledProcessError):
        mirror.fetch("1" * 40)


def _graded(tmp_path: Path, origin: Path, lab: Path) -> str:
    _git(tmp_path, "init", "-q", "--bare", str(lab))
    side = tmp_path / "side"
    _git(tmp_path, "clone", "-q", str(origin), str(side))
    head = _commit(side, "b", "b")
    _git(side, "push", "-q", f"file://{lab}", f"{head}:refs/grade/t1")
    return head


@pytest.mark.feature("lab-remote-heads")
def test_fetch_takes_a_sha_only_the_lab_remote_holds_from_the_lab_remote(tmp_path: Path, origin: Path) -> None:
    base = _commit(origin, "a", "a")
    lab = tmp_path / "lab" / "origin.git"
    head = _graded(tmp_path, origin, lab)
    mirror = replace(_mirror(tmp_path, origin, base), lab=worktrees.Lab(f"file://{lab}", "lab-token"))
    assert mirror.fetch(head) is True
    assert _git(mirror.store, "cat-file", "-t", head) == "commit\n"
    assert mirror.fetch(head) is False


@pytest.mark.feature("lab-remote-heads")
def test_fetch_never_asks_a_lab_remote_of_another_repository(tmp_path: Path, origin: Path) -> None:
    base = _commit(origin, "a", "a")
    lab = tmp_path / "lab" / "widgets.git"
    head = _graded(tmp_path, origin, lab)
    mirror = replace(_mirror(tmp_path, origin, base), lab=worktrees.Lab(f"file://{lab}", "lab-token"))
    with pytest.raises(subprocess.CalledProcessError):
        mirror.fetch(head)


@pytest.mark.feature("lab-remote-heads")
def test_a_lab_fetch_sends_the_token_as_the_lab_user_s_basic_password_for_the_lab_url_only(
    tmp_path: Path, origin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = _commit(origin, "a", "a")
    mirror = replace(_mirror(tmp_path, origin, base), lab=worktrees.Lab("https://lab.example/origin.git", "lab-token"))
    assert mirror.fetch(base) is False
    calls: list[tuple[list[str], dict[str, str] | None]] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs.get("env")))
        if "fetch" in argv and len(calls) == 2:
            raise subprocess.CalledProcessError(128, argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(worktrees.subprocess, "run", run)
    assert mirror.fetch("1" * 40) is True
    store = str(mirror.store)
    assert [argv for argv, _ in calls[1:]] == [
        [
            "git",
            "-C",
            store,
            "fetch",
            "--quiet",
            "--no-write-fetch-head",
            f"file://{origin}",
            f"{'1' * 40}:refs/lab/{'1' * 40}",
        ],
        [
            "git",
            "-C",
            store,
            "fetch",
            "--quiet",
            "--no-write-fetch-head",
            "https://lab.example/origin.git",
            f"{'1' * 40}:refs/lab/{'1' * 40}",
        ],
    ]
    assert calls[1][1] is None
    assert calls[2][1] == {
        **os.environ,
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://lab.example/origin.git.extraheader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {base64.b64encode(b'lab:lab-token').decode()}",
        "GIT_TERMINAL_PROMPT": "0",
    }


@pytest.mark.feature("lab-remote-heads")
def test_the_lab_remote_comes_from_the_environment_and_never_shows_its_token() -> None:
    environ = {
        "AGENTIHOOKS_LAB_REMOTE": "https://lab.example/git/origin.git",
        "AGENTIHOOKS_LAB_REMOTE_TOKEN": "lab-token",
    }
    found = worktrees.lab(environ)
    assert found == worktrees.Lab("https://lab.example/git/origin.git", "lab-token")
    assert "lab-token" not in repr(found)
    assert worktrees.lab({"AGENTIHOOKS_LAB_REMOTE_TOKEN": "lab-token"}) is None
    assert worktrees.lab({"AGENTIHOOKS_LAB_REMOTE": "https://lab.example/git/origin.git"}) == worktrees.Lab(
        "https://lab.example/git/origin.git", ""
    )


def test_a_second_lease_of_the_same_head_reuses_the_tree(
    tmp_path: Path, origin: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    head = _commit(origin, "a", "a")
    trees = worktrees.Worktrees(mirror=_mirror(tmp_path, origin, head), root=tmp_path / "trees", capacity=2)
    first, cut = trees.lease(head)
    second, reused = trees.lease(head)
    assert first == second == tmp_path / "trees" / head
    assert (cut, reused) == (False, False)
    assert trees.live == OrderedDict({head: 2})
    assert _registered(trees.mirror.store) == [str(first)]
    assert (first / "a").read_text() == "a"
    assert _git(first, "rev-parse", "HEAD").strip() == head
    assert _git(first, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD\n"
    assert capfd.readouterr() == ("", "")


def test_a_leased_tree_is_read_only_and_leaves_symlink_targets_alone(tmp_path: Path, origin: Path) -> None:
    outside = tmp_path / "outside"
    outside.write_text("outside")
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "file").write_text("file")
    (origin / "sub").mkdir()
    (origin / "sub" / "link").symlink_to(outside)
    (origin / "dirlink").symlink_to(tmp_path / "elsewhere")
    _git(origin, "add", "sub/link", "dirlink")
    head = _commit(origin, "a", "a")
    trees = worktrees.Worktrees(mirror=_mirror(tmp_path, origin, head), root=tmp_path / "trees", capacity=1)
    umask = os.umask(0)
    try:
        tree, _ = trees.lease(head)
    finally:
        os.umask(umask)
    assert _writable(tree) == ["dirlink", "sub/link"]
    assert outside.stat().st_mode & stat.S_IWUSR
    assert (tmp_path / "elsewhere").stat().st_mode & stat.S_IWUSR
    assert (tmp_path / "elsewhere" / "file").stat().st_mode & stat.S_IWUSR


def test_idle_trees_within_capacity_stay(tmp_path: Path, origin: Path) -> None:
    first = _commit(origin, "a", "a")
    second = _commit(origin, "b", "b")
    trees = worktrees.Worktrees(mirror=_mirror(tmp_path, origin, second), root=tmp_path / "trees", capacity=3)
    for head in (first, second):
        trees.lease(head)
        trees.release(head)
    assert trees.live == OrderedDict({first: 0, second: 0})
    assert _registered(trees.mirror.store) == sorted(str(tmp_path / "trees" / head) for head in (first, second))


def test_an_idle_tree_at_exact_capacity_stays(tmp_path: Path, origin: Path) -> None:
    first = _commit(origin, "a", "a")
    second = _commit(origin, "b", "b")
    trees = worktrees.Worktrees(mirror=_mirror(tmp_path, origin, second), root=tmp_path / "trees", capacity=2)
    trees.lease(first)
    trees.release(first)
    trees.lease(second)
    assert trees.live == OrderedDict({first: 0, second: 1})
    assert (tmp_path / "trees" / first / "a").is_file()


def test_the_least_recently_leased_idle_tree_goes_first(tmp_path: Path, origin: Path) -> None:
    heads = [_commit(origin, name, name) for name in ("a", "b", "c")]
    trees = worktrees.Worktrees(mirror=_mirror(tmp_path, origin, heads[2]), root=tmp_path / "trees", capacity=2)
    for head in (heads[0], heads[1], heads[0]):
        trees.lease(head)
        trees.release(head)
    trees.lease(heads[2])
    assert trees.live == OrderedDict({heads[0]: 0, heads[2]: 1})
    assert not (tmp_path / "trees" / heads[1]).exists()
    assert _registered(trees.mirror.store) == sorted(str(tmp_path / "trees" / head) for head in (heads[0], heads[2]))


def test_a_tree_with_live_units_is_never_evicted(tmp_path: Path, origin: Path) -> None:
    first = _commit(origin, "a", "a")
    second = _commit(origin, "b", "b")
    trees = worktrees.Worktrees(mirror=_mirror(tmp_path, origin, second), root=tmp_path / "trees", capacity=1)
    trees.lease(first)
    trees.lease(second)
    assert trees.live == OrderedDict({first: 1, second: 1})
    assert (tmp_path / "trees" / first / "a").is_file()
    trees.release(first)
    assert trees.live == OrderedDict({second: 1})
    assert not (tmp_path / "trees" / first).exists()
    assert trees.lease(first) == (tmp_path / "trees" / first, False)
    assert (tmp_path / "trees" / first / "a").read_text() == "a"


def test_a_failed_lease_records_no_tree(tmp_path: Path, origin: Path) -> None:
    trees = worktrees.Worktrees(
        mirror=_mirror(tmp_path, origin, _commit(origin, "a", "a")), root=tmp_path / "trees", capacity=1
    )
    with pytest.raises(subprocess.CalledProcessError):
        trees.lease("1" * 40)
    assert trees.live == OrderedDict()


def test_a_restarted_worker_recuts_a_tree_its_predecessor_left(tmp_path: Path, origin: Path) -> None:
    head = _commit(origin, "a", "a")
    mirror = _mirror(tmp_path, origin, head)
    worktrees.Worktrees(mirror=mirror, root=tmp_path / "trees", capacity=1).lease(head)
    trees = worktrees.Worktrees(mirror=mirror, root=tmp_path / "trees", capacity=1)
    tree, _ = trees.lease(head)
    assert trees.live == OrderedDict({head: 1})
    assert (tree / "a").read_text() == "a"
    assert _writable(tree) == []
    assert _registered(mirror.store) == [str(tree)]


def test_a_lease_replaces_an_unregistered_directory_at_the_head_path(tmp_path: Path, origin: Path) -> None:
    head = _commit(origin, "a", "a")
    trees = worktrees.Worktrees(mirror=_mirror(tmp_path, origin, head), root=tmp_path / "trees", capacity=1)
    (tmp_path / "trees" / head / "deep").mkdir(parents=True)
    (tmp_path / "trees" / head / "deep" / "left").write_text("left")
    (tmp_path / "trees" / head / "deep").chmod(0o500)
    tree, _ = trees.lease(head)
    assert sorted(path.name for path in tree.iterdir()) == [".git", "a"]
    assert _registered(trees.mirror.store) == [str(tree)]


def test_a_mutation_unit_gets_its_own_writable_copy(
    tmp_path: Path, origin: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    head = _commit(origin, "a", "a")
    trees = worktrees.Worktrees(mirror=_mirror(tmp_path, origin, head), root=tmp_path / "pod" / "trees", capacity=1)
    own, _ = trees.own(head)
    shared, _ = trees.lease(head)
    other, _ = trees.own(head)
    assert len({shared, own, other}) == 3
    assert own.parent == tmp_path / "pod" / "trees"
    assert own.name.startswith(f"{head}.")
    assert _git(own, "rev-parse", "HEAD").strip() == head
    assert _writable(own) == sorted(str(path.relative_to(own)) for path in (own, *own.rglob("*")))
    (own / "a").write_text("mutant")
    assert (shared / "a").read_text() == "a"
    assert trees.live == OrderedDict({head: 1})
    trees.drop(own)
    assert not own.exists()
    assert _registered(trees.mirror.store) == sorted([str(shared), str(other)])
    assert capfd.readouterr() == ("", "")


@pytest.mark.feature("lab-scheduler-metrics")
def test_a_lease_and_an_own_copy_report_whether_the_store_fetched_their_head(tmp_path: Path, origin: Path) -> None:
    base = _commit(origin, "a", "a")
    head = _commit(origin, "b", "b")
    trees = worktrees.Worktrees(mirror=_mirror(tmp_path, origin, base), root=tmp_path / "trees", capacity=2)
    assert trees.lease(base) == (tmp_path / "trees" / base, False)
    assert trees.lease(head) == (tmp_path / "trees" / head, True)
    assert trees.lease(head) == (tmp_path / "trees" / head, False)
    assert trees.own(head)[1] is False
    later = _commit(origin, "c", "c")
    owned, fetched = trees.own(later)
    assert fetched is True
    assert _git(owned, "rev-parse", "HEAD").strip() == later
