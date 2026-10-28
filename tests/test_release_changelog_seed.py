import io
import json
import subprocess

import pytest

from scripts import release_changelog as rc
from scripts import release_changelog_seed as seed

pytestmark = pytest.mark.unit


def _words(n: int) -> str:
    return " ".join(["word"] * n)


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    for var, value in {
        "GIT_AUTHOR_NAME": "a",
        "GIT_AUTHOR_EMAIL": "a@x",
        "GIT_COMMITTER_NAME": "a",
        "GIT_COMMITTER_EMAIL": "a@x",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
    }.items():
        monkeypatch.setenv(var, value)
    _day(monkeypatch, "2026-09-01")
    _git(tmp_path, "init", "-q", "-b", "dev")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _day(monkeypatch, day):
    monkeypatch.setenv("GIT_AUTHOR_DATE", f"{day}T10:00:00Z")
    monkeypatch.setenv("GIT_COMMITTER_DATE", f"{day}T10:00:00Z")


def _commit(repo, subject, name="f"):
    (repo / name).write_text(subject + "\n")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", subject)
    return _git(repo, "rev-parse", "HEAD")


def _promote(repo, graded, tag, graded_line=True):
    tree = _git(repo, "rev-parse", f"{graded}^{{tree}}")
    body = f"Promote green dev to main\n\nGraded dev revision: {graded}" if graded_line else "Promote green dev to main"
    sha = _git(repo, "commit-tree", tree, "-m", body)
    _git(repo, "tag", tag, sha)
    return sha


def test_stable_tags_order_by_version_and_drop_prereleases():
    assert seed.stable_tags(["v2.17.10", "v2.17.9", "v2.17.11rc1", "latest", "v2.16.200"]) == [
        "v2.16.200",
        "v2.17.9",
        "v2.17.10",
    ]


def test_release_revision_reads_the_graded_line(repo):
    graded = _commit(repo, "One (#1)")
    _promote(repo, graded, "v1.0.1")
    assert seed.release_revision("v1.0.1", {}) == graded


def test_release_revision_without_a_graded_line_matches_the_dev_tree(repo):
    first = _commit(repo, "One (#1)")
    _commit(repo, "Two (#2)", "g")
    _promote(repo, first, "v1.0.1", graded_line=False)
    assert seed.release_revision("v1.0.1", seed.dev_trees("dev")) == first


def test_release_revision_refuses_a_tag_matching_no_dev_tree(repo):
    _promote(repo, _commit(repo, "One (#1)"), "v1.0.1", graded_line=False)
    with pytest.raises(ValueError, match="^v1.0.1 matches no dev revision$"):
        seed.release_revision("v1.0.1", {})


def test_window_releases_groups_by_first_release_and_drops_older_changes(repo, monkeypatch):
    _promote(repo, _commit(repo, "Old (#1)"), "v1.0.1")
    _day(monkeypatch, "2026-09-20")
    _commit(repo, "Before window (#2)", "b")
    _day(monkeypatch, "2026-10-01")
    second = _commit(repo, "In window (#3)", "c")
    _promote(repo, second, "v1.0.2")
    _promote(repo, second, "v1.0.3")
    third = _commit(repo, "Later (#4)", "d")
    _promote(repo, third, "v1.0.10")
    _commit(repo, "Unreleased (#5)", "e")
    releases = seed.window_releases(["v1.0.1", "v1.0.2", "v1.0.3", "v1.0.10"], {}, "2026-09-28")
    assert [(tag, [c.number for c in kept]) for tag, kept in releases] == [
        ("v1.0.10", [4]),
        ("v1.0.3", []),
        ("v1.0.2", [3]),
    ]


def test_write_sections_renders_each_release_with_one_entry_per_change(repo):
    graded = _commit(repo, "Add it (#7)")
    _promote(repo, graded, "v1.0.1")
    releases = seed.window_releases(["v1.0.1"], {}, "2026-09-01")
    asked = []
    sections = seed.write_sections(
        releases,
        lambda c: asked.append(c.number) or _words(21),
        lambda query: {"repository": {"p7": {"author": {"login": "ann"}, "body": "b"}}},
        "org/repo",
        workers=2,
    )
    assert asked == [7]
    assert sections == [
        f"## v1.0.1 · 2026-09-01\n\n### 2026-09-01 · {graded[:12]} · #7 · @ann\n\n**Add it**\n\n{_words(21)}\n"
    ]


def test_merge_changelog_keeps_written_releases_and_puts_the_newest_first():
    existing = rc.prepend_release("", "## v1.0.10 · d\n\nkept\n")
    merged = seed.merge_changelog(existing, ["## v1.0.10 · d\n\nnew\n", "## v1.0.9 · c\n\nx\n", "## v1.0.2 · b\n\ny\n"])
    assert merged == (
        f"{rc.TITLE}\n\n{rc.INTRO}\n\n## v1.0.10 · d\n\nkept\n\n## v1.0.9 · c\n\nx\n\n## v1.0.2 · b\n\ny\n"
    )


def test_merged_count_asks_search_for_pull_requests_merged_into_dev(monkeypatch):
    seen = []
    monkeypatch.setattr(rc, "run", lambda *command: seen.append(command) or "42\n")
    assert seed.merged_count("org/repo", "2026-09-28", "2026-10-28T12:00:00+00:00") == 42
    assert seen == [
        (
            "gh",
            "api",
            "-X",
            "GET",
            "search/issues",
            "-f",
            "q=repo:org/repo is:pr is:merged base:dev merged:2026-09-28..2026-10-28T12:00:00+00:00",
            "--jq",
            ".total_count",
        )
    ]


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Today(seed.date):
    @classmethod
    def today(cls):
        return cls(2026, 10, 28)


def _seed_repo(repo, monkeypatch):
    _promote(repo, _commit(repo, "Old (#1)"), "v1.0.1")
    _day(monkeypatch, "2026-10-20")
    _commit(repo, "Direct fix", "h")
    graded = _commit(repo, "Add the seed (#5)", "g")
    _promote(repo, graded, "v1.0.2")
    monkeypatch.setattr(seed, "date", _Today)
    monkeypatch.setattr(
        seed,
        "urlopen",
        lambda request, timeout: _Response(json.dumps({"choices": [{"message": {"content": _words(30)}}]}).encode()),
    )
    monkeypatch.setattr(
        rc, "gh_graphql", lambda query: {"repository": {"p5": {"author": {"login": "dev"}, "body": ""}}}
    )
    monkeypatch.setenv(rc.KEY_VAR, "k")
    monkeypatch.setenv("GITHUB_REPOSITORY", "org/repo")
    return graded


def test_main_seeds_the_window_and_reports_matching_counts(repo, monkeypatch, tmp_path, capsys):
    graded = _seed_repo(repo, monkeypatch)
    windows = []
    monkeypatch.setattr(seed, "merged_count", lambda *a: windows.append(a) or 1)
    changelog = tmp_path / "RELEASES.md"
    assert seed.main([str(changelog), "--dev", "dev"]) == 0
    text = changelog.read_text()
    assert text.startswith(f"{rc.TITLE}\n\n{rc.INTRO}\n\n## v1.0.2 · 2026-10-20\n\n")
    assert f"· {graded[:12]} · #5 · @dev\n\n**Add the seed**\n\n{_words(30)}\n" in text
    assert "· direct commit · @unknown\n\n**Direct fix**" in text
    assert "Old" not in text
    assert windows == [("org/repo", "2026-09-28", "2026-10-20T10:00:00+00:00")]
    out = capsys.readouterr().out
    assert "entries: 2 (pull requests 1, direct commits 1)\n" in out
    assert "merged pull requests into dev in window: 1\n" in out


def test_main_fails_when_the_counts_differ(repo, monkeypatch, tmp_path, capsys):
    _seed_repo(repo, monkeypatch)
    monkeypatch.setattr(seed, "merged_count", lambda *a: 2)
    assert seed.main([str(tmp_path / "RELEASES.md"), "--dev", "dev"]) == 1
    assert "merged pull requests into dev in window: 2\n" in capsys.readouterr().out
