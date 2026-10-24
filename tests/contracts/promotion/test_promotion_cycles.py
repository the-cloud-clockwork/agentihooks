import subprocess


def _git(root, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=ci", "-c", "user.email=ci@example.com", *args],
        capture_output=True,
        text=True,
        check=check,
    )


def _commit_on_dev(root, text):
    (root / "a.txt").write_text(text)
    _git(root, "commit", "-qam", text)
    return _git(root, "rev-parse", "HEAD").stdout.strip()


def _squash(root, main, tree):
    return _git(root, "commit-tree", tree, "-p", main, "-m", "Promote green dev to main").stdout.strip()


def _tree(root, rev):
    return _git(root, "rev-parse", f"{rev}^{{tree}}").stdout.strip()


def _merges(root, main, head):
    _git(root, "switch", "-q", "--detach", main)
    merged = _git(root, "merge", "-q", "--no-ff", "--no-commit", head, check=False).returncode == 0
    _git(root, "merge", "--abort", check=False)
    _git(root, "switch", "-q", "dev")
    return merged


def test_snapshots_on_main_keep_later_promotions_mergeable(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "dev")
    (tmp_path / "a.txt").write_text("base\n")
    _git(tmp_path, "add", "a.txt")
    _git(tmp_path, "commit", "-qm", "base")
    main = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

    first = _commit_on_dev(tmp_path, "first\n")
    main = _squash(tmp_path, main, _tree(tmp_path, first))
    second = _commit_on_dev(tmp_path, "second\n")
    assert not _merges(tmp_path, main, second)

    for graded in (second, _commit_on_dev(tmp_path, "third\n")):
        snapshot = _squash(tmp_path, main, _tree(tmp_path, graded))
        assert _merges(tmp_path, main, snapshot)
        main = _squash(tmp_path, main, _tree(tmp_path, snapshot))
        assert _tree(tmp_path, main) == _tree(tmp_path, graded)
