"""Unit tests for scripts.memory_mirror_sync — cross-machine memory sync."""

from __future__ import annotations

import subprocess

from scripts import memory_mirror_sync as mm

# ---------------------------------------------------------------------------
# Filter spec — the critical guard that keeps transcripts/sessions out.
# ---------------------------------------------------------------------------


def test_rsync_filter_excludes_everything_but_memory():
    """Any leak of non-memory paths is a P0 bug."""
    assert mm.RSYNC_MEMORY_FILTER == [
        "--prune-empty-dirs",
        "--include=*/",
        "--include=*/memory/",
        "--include=*/memory/**",
        "--exclude=*",
    ]


def test_snapshot_in_invokes_rsync_with_memory_only_filter(tmp_path, monkeypatch):
    src = tmp_path / "projects"
    src.mkdir()
    (src / "proj-a").mkdir()
    monkeypatch.setattr(mm.config, "MEMORY_MIRROR_CLAUDE_PROJECTS", str(src))
    monkeypatch.setattr(mm.config, "MEMORY_MIRROR_DIR", str(tmp_path / "mirror"))

    captured: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(mm, "_run", fake_run)
    mm.snapshot_in()

    assert len(captured) == 1
    cmd = captured[0]
    assert cmd[0] == "rsync"
    assert "-a" in cmd and "--delete" in cmd
    for flag in mm.RSYNC_MEMORY_FILTER:
        assert flag in cmd
    assert cmd[-2].endswith("/projects/")
    assert cmd[-1].endswith("/mirror/")


def test_snapshot_in_skips_when_source_missing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        mm.config, "MEMORY_MIRROR_CLAUDE_PROJECTS", str(tmp_path / "nope")
    )
    monkeypatch.setattr(mm.config, "MEMORY_MIRROR_DIR", str(tmp_path / "mirror"))

    called = []
    monkeypatch.setattr(mm, "_run", lambda *a, **kw: called.append(a))
    mm.snapshot_in()

    assert called == []
    out = capsys.readouterr().out
    assert "SKIP snapshot" in out


# ---------------------------------------------------------------------------
# Self-branch filtering — prevents fetch/merge loops.
# ---------------------------------------------------------------------------


def test_is_self_matches_local_hostname(monkeypatch):
    monkeypatch.setattr(mm, "_hostname", lambda: "alpha")
    monkeypatch.setattr(mm.config, "MEMORY_MIRROR_BRANCH_PREFIX", "gitfoam")

    assert mm._is_self("gitfoam/alpha/main") is True
    assert mm._is_self("gitfoam/alpha/feature") is True


def test_is_self_rejects_other_hosts(monkeypatch):
    monkeypatch.setattr(mm, "_hostname", lambda: "alpha")
    monkeypatch.setattr(mm.config, "MEMORY_MIRROR_BRANCH_PREFIX", "gitfoam")

    assert mm._is_self("gitfoam/beta/main") is False
    assert mm._is_self("gitfoam/gamma/dev") is False


def test_is_self_respects_branch_prefix(monkeypatch):
    monkeypatch.setattr(mm, "_hostname", lambda: "alpha")
    monkeypatch.setattr(mm.config, "MEMORY_MIRROR_BRANCH_PREFIX", "mirror")

    assert mm._is_self("mirror/alpha/main") is True
    assert mm._is_self("gitfoam/alpha/main") is False


# ---------------------------------------------------------------------------
# Conflict filename format — must encode host + epoch + original extension.
# ---------------------------------------------------------------------------


def test_conflict_filename_preserves_extension_and_stem(monkeypatch, tmp_path):
    monkeypatch.setattr(mm, "_hostname", lambda: "alpha")
    monkeypatch.setattr(mm.time, "time", lambda: 1700000000)

    target = tmp_path / "memory" / "MEMORY.md"
    conflict = mm._conflict_filename(target)
    assert conflict.name == "MEMORY.conflict-alpha-1700000000.md"
    assert conflict.parent == target.parent


def test_conflict_filename_handles_no_extension(monkeypatch, tmp_path):
    monkeypatch.setattr(mm, "_hostname", lambda: "beta")
    monkeypatch.setattr(mm.time, "time", lambda: 1700000001)
    target = tmp_path / "memory" / "notes"
    conflict = mm._conflict_filename(target)
    assert conflict.name == "notes.conflict-beta-1700000001"


# ---------------------------------------------------------------------------
# Merge semantics — byte-equal files no-op, differing files produce conflict.
# ---------------------------------------------------------------------------


def test_merge_tree_copies_new_files(tmp_path):
    staging = tmp_path / "staging"
    target = tmp_path / "target"
    (staging / "proj/memory").mkdir(parents=True)
    (staging / "proj/memory/MEMORY.md").write_text("hello")

    mm._merge_tree(staging, target)

    assert (target / "proj/memory/MEMORY.md").read_text() == "hello"


def test_merge_tree_noop_on_identical(tmp_path):
    staging = tmp_path / "staging"
    target = tmp_path / "target"
    (staging / "proj/memory").mkdir(parents=True)
    (target / "proj/memory").mkdir(parents=True)
    (staging / "proj/memory/MEMORY.md").write_text("same")
    (target / "proj/memory/MEMORY.md").write_text("same")

    mm._merge_tree(staging, target)

    # No conflict sibling should exist.
    siblings = list((target / "proj/memory").iterdir())
    assert len(siblings) == 1
    assert siblings[0].name == "MEMORY.md"


def test_merge_tree_writes_conflict_on_divergence(tmp_path, monkeypatch):
    monkeypatch.setattr(mm, "_hostname", lambda: "alpha")
    monkeypatch.setattr(mm.time, "time", lambda: 1700000002)

    staging = tmp_path / "staging"
    target = tmp_path / "target"
    (staging / "proj/memory").mkdir(parents=True)
    (target / "proj/memory").mkdir(parents=True)
    (staging / "proj/memory/MEMORY.md").write_text("remote")
    (target / "proj/memory/MEMORY.md").write_text("local")

    mm._merge_tree(staging, target)

    assert (target / "proj/memory/MEMORY.md").read_text() == "local"  # untouched
    assert (
        target / "proj/memory/MEMORY.conflict-alpha-1700000002.md"
    ).read_text() == "remote"


# ---------------------------------------------------------------------------
# tick() gating — must no-op when disabled or remote unset.
# ---------------------------------------------------------------------------


def test_tick_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(mm.config, "MEMORY_MIRROR_ENABLED", False)
    monkeypatch.setattr(mm.config, "MEMORY_MIRROR_REMOTE", "git@host:repo.git")

    called = []
    monkeypatch.setattr(mm, "ensure_mirror_repo", lambda: called.append("ensure"))
    monkeypatch.setattr(mm, "snapshot_in", lambda: called.append("snap"))
    monkeypatch.setattr(mm, "fetch_remote", lambda: called.append("fetch"))
    monkeypatch.setattr(mm, "consume_remote_branches", lambda: called.append("merge"))

    mm.tick()
    assert called == []


def test_tick_skips_when_remote_unset(monkeypatch, capsys):
    monkeypatch.setattr(mm.config, "MEMORY_MIRROR_ENABLED", True)
    monkeypatch.setattr(mm.config, "MEMORY_MIRROR_REMOTE", "")

    mm.tick()
    assert "MEMORY_MIRROR_REMOTE not set" in capsys.readouterr().out


def test_tick_runs_full_pipeline_when_enabled(monkeypatch):
    monkeypatch.setattr(mm.config, "MEMORY_MIRROR_ENABLED", True)
    monkeypatch.setattr(mm.config, "MEMORY_MIRROR_REMOTE", "git@host:repo.git")

    order = []
    monkeypatch.setattr(mm, "ensure_mirror_repo", lambda: order.append("ensure"))
    monkeypatch.setattr(mm, "snapshot_in", lambda: order.append("snap"))
    monkeypatch.setattr(mm, "fetch_remote", lambda: order.append("fetch"))
    monkeypatch.setattr(mm, "consume_remote_branches", lambda: order.append("merge"))

    mm.tick()
    assert order == ["ensure", "snap", "fetch", "merge"]


# ---------------------------------------------------------------------------
# Remote branch parsing — short-ref extraction.
# ---------------------------------------------------------------------------


def test_list_remote_branches_strips_origin_prefix(monkeypatch, tmp_path):
    mirror = tmp_path / "mirror"
    (mirror / ".git").mkdir(parents=True)
    monkeypatch.setattr(mm, "_mirror_dir", lambda: mirror)
    monkeypatch.setattr(mm.config, "MEMORY_MIRROR_BRANCH_PREFIX", "gitfoam")

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout="origin/gitfoam/alpha/main\norigin/gitfoam/beta/main\n",
            stderr="",
        )

    monkeypatch.setattr(mm, "_run", fake_run)
    branches = mm._list_remote_branches()
    assert branches == ["gitfoam/alpha/main", "gitfoam/beta/main"]
