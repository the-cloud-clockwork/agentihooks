"""Unit tests for scripts.memory_mirror_sync — v2 PR-gated fleet propagation."""

from __future__ import annotations

import importlib
import os
import subprocess
from pathlib import Path

from scripts import memory_mirror_sync as mm

# ---------------------------------------------------------------------------
# v3 Identity resolver — decoder, boundary, key, map.
# ---------------------------------------------------------------------------


def _make_fs(root: Path, layout: dict) -> None:
    """Build a synthetic filesystem from a nested dict.
    Values that are dicts become subdirs; str values become files with that
    content; `None` becomes an empty file."""
    root.mkdir(parents=True, exist_ok=True)
    for name, val in layout.items():
        target = root / name
        if isinstance(val, dict):
            _make_fs(target, val)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(val if val is not None else "")


def test_decode_roundtrips_simple_path(tmp_path):
    _make_fs(tmp_path, {"home": {"alice": {"dev": {"proj": {}}}}})
    encoded = "-home-alice-dev-proj"
    assert mm._decode_encoded_path(encoded, root=tmp_path) == tmp_path / "home/alice/dev/proj"


def test_decode_handles_hyphenated_segment(tmp_path):
    _make_fs(tmp_path, {"home": {"alice": {"dev": {"tccw-ecosystem": {"tccw-toolbelt": {}}}}}})
    encoded = "-home-alice-dev-tccw-ecosystem-tccw-toolbelt"
    assert mm._decode_encoded_path(encoded, root=tmp_path) == (
        tmp_path / "home/alice/dev/tccw-ecosystem/tccw-toolbelt"
    )


def test_decode_returns_none_when_path_missing(tmp_path):
    _make_fs(tmp_path, {"home": {"alice": {}}})
    assert mm._decode_encoded_path("-home-alice-nowhere", root=tmp_path) is None


def test_boundary_prefers_agent_yml_over_pyproject(tmp_path):
    # Layout: finops/ has agent.yml; finops/package/ has pyproject.toml.
    _make_fs(tmp_path, {
        "agents": {
            "finops": {
                "agent.yml": "name: finops",
                "package": {
                    "pyproject.toml": "[project]\nname='pkg'",
                    "src": {},
                },
            },
        },
    })
    real = tmp_path / "agents/finops/package"
    boundary = mm._package_boundary(real)
    assert boundary is not None
    path, marker = boundary
    assert path == tmp_path / "agents/finops"
    assert marker == "agent.yml"


def test_boundary_stops_at_git(tmp_path):
    _make_fs(tmp_path, {
        "repo": {
            ".git": {"HEAD": "ref: refs/heads/main\n"},
            "pyproject.toml": "[project]\nname='r'",
            "subdir": {},
        },
    })
    real = tmp_path / "repo/subdir"
    boundary = mm._package_boundary(real)
    # pyproject.toml at repo/ wins over .git by priority, but we must not
    # walk past repo/ (don't cross .git).
    assert boundary is not None
    path, marker = boundary
    assert path == tmp_path / "repo"
    assert marker == "pyproject.toml"


def test_boundary_git_fallback_when_no_package_markers(tmp_path):
    _make_fs(tmp_path, {
        "repo": {
            ".git": {"HEAD": "ref: refs/heads/main\n"},
            "src": {},
        },
    })
    real = tmp_path / "repo/src"
    boundary = mm._package_boundary(real)
    assert boundary is not None
    path, marker = boundary
    assert path == tmp_path / "repo"
    assert marker == ".git"


def test_identity_key_ok_via_helpers(tmp_path):
    """Integration: decode + boundary → identity basename."""
    _make_fs(tmp_path, {"home": {"alice": {"dev": {"myrepo": {
        ".git": {"HEAD": "ref\n"},
        "pyproject.toml": "[project]\nname='myrepo'",
    }}}}})
    real = mm._decode_encoded_path("-home-alice-dev-myrepo", root=tmp_path)
    assert real is not None and real.name == "myrepo"
    boundary = mm._package_boundary(real)
    assert boundary is not None
    assert boundary[0].name == "myrepo"


def test_identity_key_unmapped_when_decode_fails(monkeypatch):
    monkeypatch.setattr(mm, "_decode_encoded_path", lambda *a, **kw: None)
    key, status = mm._identity_key("-phantom-encoded-path")
    assert status == "unmapped"
    assert key == "-phantom-encoded-path"


def test_identity_map_groups_by_key(tmp_path, monkeypatch):
    # Create two "encoded" projects under a fake ~/.claude/projects/.
    projects = tmp_path / "projects"
    projects.mkdir()
    (projects / "-home-alice-dev-finops-package").mkdir()
    (projects / "-home-alice-dev-finops-other-dir").mkdir()
    monkeypatch.setattr(mm.config, "MEMORY_MIRROR_CLAUDE_PROJECTS", str(projects))
    # Fake resolver: map both encoded dirs to the same identity "finops".
    monkeypatch.setattr(
        mm,
        "_identity_key",
        lambda encoded: ("finops", "ok")
        if "finops" in encoded else (encoded, "unmapped"),
    )
    id_map = mm._identity_map()
    assert sorted(id_map["finops"]) == [
        "-home-alice-dev-finops-other-dir",
        "-home-alice-dev-finops-package",
    ]


# ---------------------------------------------------------------------------
# v3 snapshot_in — per-identity rsync into by-project/<key>/memory/
# ---------------------------------------------------------------------------


def test_snapshot_in_writes_by_project_layout(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    mirror = tmp_path / "mirror"
    (projects / "-x-finops" / "memory").mkdir(parents=True)
    (projects / "-x-finops" / "memory" / "MEMORY.md").write_text("finops mem")
    (projects / "-x-publisher" / "memory").mkdir(parents=True)
    (projects / "-x-publisher" / "memory" / "MEMORY.md").write_text("publisher mem")
    monkeypatch.setattr(mm.config, "MEMORY_MIRROR_CLAUDE_PROJECTS", str(projects))
    monkeypatch.setattr(mm.config, "MEMORY_MIRROR_DIR", str(mirror))
    monkeypatch.setattr(mm, "_identity_key", lambda enc: (
        ("finops", "ok") if "finops" in enc else ("publisher", "ok")
    ))

    # Use real rsync so we exercise the actual file copy.
    mm.snapshot_in()

    assert (mirror / "by-project/finops/memory/MEMORY.md").read_text() == "finops mem"
    assert (mirror / "by-project/publisher/memory/MEMORY.md").read_text() == "publisher mem"
    assert not (mirror / "_unmapped").exists() or not any((mirror / "_unmapped").iterdir())


def test_snapshot_in_buckets_unmapped(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    mirror = tmp_path / "mirror"
    (projects / "-phantom" / "memory").mkdir(parents=True)
    (projects / "-phantom" / "memory" / "MEMORY.md").write_text("orphan")
    monkeypatch.setattr(mm.config, "MEMORY_MIRROR_CLAUDE_PROJECTS", str(projects))
    monkeypatch.setattr(mm.config, "MEMORY_MIRROR_DIR", str(mirror))
    monkeypatch.setattr(mm, "_identity_key", lambda enc: (enc, "unmapped"))

    mm.snapshot_in()

    assert (mirror / "_unmapped/-phantom/memory/MEMORY.md").read_text() == "orphan"
    assert not (mirror / "by-project").exists() or not any(
        d for d in (mirror / "by-project").iterdir() if d.is_dir()
    )


def test_snapshot_in_skips_when_source_missing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        mm.config, "MEMORY_MIRROR_CLAUDE_PROJECTS", str(tmp_path / "nope")
    )
    monkeypatch.setattr(mm.config, "MEMORY_MIRROR_DIR", str(tmp_path / "mirror"))

    called = []
    monkeypatch.setattr(mm, "_run", lambda *a, **kw: called.append(a))
    mm.snapshot_in()
    assert called == []
    assert "SKIP snapshot" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Conflict filename + merge semantics (unchanged from v1).
# ---------------------------------------------------------------------------


def test_conflict_filename_preserves_extension_and_stem(monkeypatch, tmp_path):
    monkeypatch.setattr(mm, "_hostname", lambda: "alpha")
    monkeypatch.setattr(mm.time, "time", lambda: 1700000000)

    target = tmp_path / "memory" / "MEMORY.md"
    conflict = mm._conflict_filename(target)
    assert conflict.name == "MEMORY.conflict-alpha-1700000000.md"


def test_conflict_filename_handles_no_extension(monkeypatch, tmp_path):
    monkeypatch.setattr(mm, "_hostname", lambda: "beta")
    monkeypatch.setattr(mm.time, "time", lambda: 1700000001)
    conflict = mm._conflict_filename(tmp_path / "notes")
    assert conflict.name == "notes.conflict-beta-1700000001"


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
    siblings = list((target / "proj/memory").iterdir())
    assert len(siblings) == 1


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
    assert (target / "proj/memory/MEMORY.md").read_text() == "local"
    assert (
        target / "proj/memory/MEMORY.conflict-alpha-1700000002.md"
    ).read_text() == "remote"


# ---------------------------------------------------------------------------
# tick() mode gating.
# ---------------------------------------------------------------------------


def test_tick_noop_when_mode_off(monkeypatch):
    monkeypatch.setattr(mm.config, "MEMORY_MIRROR_MODE", "off")
    monkeypatch.setattr(mm.config, "MEMORY_MIRROR_REMOTE", "git@host:r.git")

    called = []
    monkeypatch.setattr(mm, "ensure_mirror_repo", lambda: called.append("ensure"))
    monkeypatch.setattr(mm, "snapshot_in", lambda: called.append("snap"))
    monkeypatch.setattr(mm, "fetch_remote", lambda: called.append("fetch"))
    monkeypatch.setattr(mm, "consume_main", lambda: called.append("merge"))

    mm.tick()
    assert called == []


def test_tick_write_mode_full_pipeline(monkeypatch):
    monkeypatch.setattr(mm.config, "MEMORY_MIRROR_MODE", "write")
    monkeypatch.setattr(mm.config, "MEMORY_MIRROR_REMOTE", "git@host:r.git")

    order = []
    monkeypatch.setattr(mm, "ensure_mirror_repo", lambda: order.append("ensure"))
    monkeypatch.setattr(mm, "snapshot_in", lambda: order.append("snap"))
    monkeypatch.setattr(mm, "fetch_remote", lambda: order.append("fetch"))
    monkeypatch.setattr(mm, "consume_main", lambda: order.append("merge"))

    mm.tick()
    assert order == ["ensure", "snap", "fetch", "merge"]


def test_tick_write_local_only_skips_fetch_and_merge(monkeypatch):
    monkeypatch.setattr(mm.config, "MEMORY_MIRROR_MODE", "write-local-only")
    monkeypatch.setattr(mm.config, "MEMORY_MIRROR_REMOTE", "git@host:r.git")

    order = []
    monkeypatch.setattr(mm, "ensure_mirror_repo", lambda: order.append("ensure"))
    monkeypatch.setattr(mm, "snapshot_in", lambda: order.append("snap"))
    monkeypatch.setattr(mm, "fetch_remote", lambda: order.append("fetch"))
    monkeypatch.setattr(mm, "consume_main", lambda: order.append("merge"))

    mm.tick()
    assert order == ["ensure", "snap"]  # no fetch, no merge


def test_tick_skips_when_remote_unset(monkeypatch, capsys):
    monkeypatch.setattr(mm.config, "MEMORY_MIRROR_MODE", "write")
    monkeypatch.setattr(mm.config, "MEMORY_MIRROR_REMOTE", "")
    mm.tick()
    assert "MEMORY_MIRROR_REMOTE not set" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# seed_main — the v2 first-install step.
# ---------------------------------------------------------------------------


def test_seed_main_skipped_when_main_exists(monkeypatch):
    monkeypatch.setattr(mm, "ensure_mirror_repo", lambda: Path("/tmp/m"))
    monkeypatch.setattr(mm, "_remote_has_main", lambda: True)

    called = []
    monkeypatch.setattr(mm, "snapshot_in", lambda: called.append("snap"))
    monkeypatch.setattr(mm, "_run", lambda *a, **kw: called.append(a))

    assert mm.seed_main() is False
    assert called == []


def test_seed_main_creates_commit_and_pushes_when_missing(monkeypatch):
    monkeypatch.setattr(mm, "ensure_mirror_repo", lambda: Path("/tmp/m"))
    monkeypatch.setattr(mm, "_remote_has_main", lambda: False)
    monkeypatch.setattr(mm, "snapshot_in", lambda: None)
    monkeypatch.setattr(mm, "_hostname", lambda: "alpha")
    monkeypatch.setattr(mm, "_mirror_dir", lambda: Path("/tmp/m"))

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        # Return a tree SHA for write-tree, a commit SHA for commit-tree.
        if cmd[:2] == ["git", "write-tree"]:
            return subprocess.CompletedProcess(cmd, 0, "deadbeef" * 5, "")
        if cmd[:2] == ["git", "commit-tree"]:
            return subprocess.CompletedProcess(cmd, 0, "cafebabe" * 5, "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(mm, "_run", fake_run)
    assert mm.seed_main() is True

    # Assert the key steps happened in order.
    issued = [c[:3] for c in calls]
    assert ["git", "add", "-A"] in issued
    assert any(c[:2] == ["git", "write-tree"] for c in calls)
    assert any(c[:2] == ["git", "commit-tree"] for c in calls)
    assert any(c[:2] == ["git", "update-ref"] for c in calls)
    assert any(
        c[:3] == ["git", "push", "origin"]
        and "refs/heads/main:refs/heads/main" in c
        for c in calls
    )


# ---------------------------------------------------------------------------
# consume_main — noop when ref missing, merges when present.
# ---------------------------------------------------------------------------


def test_consume_main_noop_when_mirror_not_git(tmp_path, monkeypatch):
    monkeypatch.setattr(mm, "_mirror_dir", lambda: tmp_path / "nope")
    called = []
    monkeypatch.setattr(mm, "_origin_main_exists", lambda: called.append("oe") or False)
    mm.consume_main()
    # Didn't even reach origin-main check — returned on git-dir miss.
    assert called == []


def test_consume_main_noop_when_origin_main_absent(tmp_path, monkeypatch, capsys):
    mirror = tmp_path / "mirror"
    (mirror / ".git").mkdir(parents=True)
    monkeypatch.setattr(mm, "_mirror_dir", lambda: mirror)
    monkeypatch.setattr(mm, "_origin_main_exists", lambda: False)

    merge_called = []
    monkeypatch.setattr(mm, "_merge_tree", lambda *a: merge_called.append("merge"))
    mm.consume_main()
    assert merge_called == []
    assert "origin/main not present" in capsys.readouterr().out


def test_consume_main_fans_out_by_project_to_multiple_local_dirs(tmp_path, monkeypatch):
    """If origin/main's by-project/<key>/memory/ exists and N local encoded
    dirs resolve to <key>, merge the content into all N."""
    mirror = tmp_path / "mirror"
    (mirror / ".git").mkdir(parents=True)
    projects = tmp_path / "projects"
    projects.mkdir()
    (projects / "-enc-a").mkdir()
    (projects / "-enc-b").mkdir()

    monkeypatch.setattr(mm, "_mirror_dir", lambda: mirror)
    monkeypatch.setattr(mm.config, "MEMORY_MIRROR_CLAUDE_PROJECTS", str(projects))
    monkeypatch.setattr(mm, "_origin_main_exists", lambda: True)
    monkeypatch.setattr(
        mm, "_identity_map",
        lambda: {"finops": ["-enc-a", "-enc-b"]},
    )

    # Fake git archive → produce a tarball representing by-project/finops/memory/MEMORY.md.
    # Instead of mocking subprocess.Popen, build the staging tree directly by
    # monkey-patching tempfile.TemporaryDirectory to return a pre-populated dir.
    fake_staging = tmp_path / "staging"
    (fake_staging / "by-project/finops/memory").mkdir(parents=True)
    (fake_staging / "by-project/finops/memory/MEMORY.md").write_text("from main")

    class _FakeTmp:
        def __init__(self, *a, **kw):
            self.name = str(fake_staging)

        def __enter__(self):
            return self.name

        def __exit__(self, *a):
            pass

    monkeypatch.setattr(mm.tempfile, "TemporaryDirectory", _FakeTmp)

    # Fake archive + extract — already populated. Just let _merge_tree run.
    class _FakeProc:
        returncode = 0
        stdout = None

        def communicate(self):
            pass

        def wait(self):
            pass

    monkeypatch.setattr(mm.subprocess, "Popen", lambda *a, **kw: _FakeProc())

    mm.consume_main()

    assert (projects / "-enc-a/memory/MEMORY.md").read_text() == "from main"
    assert (projects / "-enc-b/memory/MEMORY.md").read_text() == "from main"


def test_consume_main_skips_identities_with_no_local_match(tmp_path, monkeypatch):
    mirror = tmp_path / "mirror"
    (mirror / ".git").mkdir(parents=True)
    projects = tmp_path / "projects"
    projects.mkdir()  # no encoded dirs at all

    monkeypatch.setattr(mm, "_mirror_dir", lambda: mirror)
    monkeypatch.setattr(mm.config, "MEMORY_MIRROR_CLAUDE_PROJECTS", str(projects))
    monkeypatch.setattr(mm, "_origin_main_exists", lambda: True)
    monkeypatch.setattr(mm, "_identity_map", lambda: {})

    fake_staging = tmp_path / "staging"
    (fake_staging / "by-project/finops/memory").mkdir(parents=True)
    (fake_staging / "by-project/finops/memory/MEMORY.md").write_text("orphan")

    class _FakeTmp:
        def __init__(self, *a, **kw):
            self.name = str(fake_staging)

        def __enter__(self):
            return self.name

        def __exit__(self, *a):
            pass

    monkeypatch.setattr(mm.tempfile, "TemporaryDirectory", _FakeTmp)

    class _FakeProc:
        returncode = 0
        stdout = None

        def communicate(self):
            pass

        def wait(self):
            pass

    monkeypatch.setattr(mm.subprocess, "Popen", lambda *a, **kw: _FakeProc())

    # Should not raise; should not create anything under projects/.
    mm.consume_main()
    assert not list(projects.iterdir())


# ---------------------------------------------------------------------------
# Remote slug derivation — needed by propose_pr for `gh pr create --repo`.
# ---------------------------------------------------------------------------


def test_remote_slug_ssh(monkeypatch):
    monkeypatch.setattr(
        mm.config,
        "MEMORY_MIRROR_REMOTE",
        "git@github.com:The-Cloud-Clock-Work/anton-memory-mirror.git",
    )
    assert mm._remote_slug() == "The-Cloud-Clock-Work/anton-memory-mirror"


def test_remote_slug_https(monkeypatch):
    monkeypatch.setattr(
        mm.config,
        "MEMORY_MIRROR_REMOTE",
        "https://github.com/owner/repo",
    )
    assert mm._remote_slug() == "owner/repo"


def test_remote_slug_non_github_returns_none(monkeypatch):
    monkeypatch.setattr(mm.config, "MEMORY_MIRROR_REMOTE", "git@gitlab.com:x/y.git")
    assert mm._remote_slug() is None


# ---------------------------------------------------------------------------
# propose_pr — noop when no diff between branch and main.
# ---------------------------------------------------------------------------


def test_propose_pr_noop_when_no_diff(monkeypatch, capsys):
    monkeypatch.setattr(
        mm.config,
        "MEMORY_MIRROR_REMOTE",
        "git@github.com:owner/repo.git",
    )
    monkeypatch.setattr(mm.shutil, "which", lambda cmd: "/usr/bin/gh")

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["git", "rev-list", "--count"]:
            return subprocess.CompletedProcess(cmd, 0, "0\n", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(mm, "_run", fake_run)
    rc = mm.propose_pr(auto_merge=False)
    assert rc == 1
    assert "nothing to propose" in capsys.readouterr().out


def test_propose_pr_noop_when_tree_matches_main(monkeypatch, capsys):
    """Seed commits + gitfoam's push yield different SHAs but identical trees —
    don't open an empty PR."""
    monkeypatch.setattr(
        mm.config, "MEMORY_MIRROR_REMOTE", "git@github.com:owner/repo.git"
    )
    monkeypatch.setattr(mm.shutil, "which", lambda cmd: "/usr/bin/gh")

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["git", "rev-list", "--count"]:
            return subprocess.CompletedProcess(cmd, 0, "1\n", "")
        if cmd[:3] == ["git", "diff", "--quiet"]:
            # 0 = no diff → tree identical
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(mm, "_run", fake_run)
    rc = mm.propose_pr(auto_merge=False)
    assert rc == 1
    assert "same tree as main" in capsys.readouterr().out


def test_propose_pr_requires_gh_cli(monkeypatch, capsys):
    monkeypatch.setattr(
        mm.config,
        "MEMORY_MIRROR_REMOTE",
        "git@github.com:owner/repo.git",
    )
    monkeypatch.setattr(mm.shutil, "which", lambda cmd: None)
    rc = mm.propose_pr()
    assert rc == 2
    assert "gh` CLI not found" in capsys.readouterr().out


def test_propose_pr_rejects_non_github_remote(monkeypatch, capsys):
    monkeypatch.setattr(mm.config, "MEMORY_MIRROR_REMOTE", "git@gitlab.com:x/y.git")
    rc = mm.propose_pr()
    assert rc == 2
    assert "not a github.com URL" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# sweep_branches — deletes only merged + idle branches.
# ---------------------------------------------------------------------------


def test_sweep_branches_deletes_merged_idle_only(tmp_path, monkeypatch):
    mirror = tmp_path / "mirror"
    (mirror / ".git").mkdir(parents=True)
    monkeypatch.setattr(mm, "_mirror_dir", lambda: mirror)
    monkeypatch.setattr(mm, "_origin_main_exists", lambda: True)
    monkeypatch.setattr(mm.config, "MEMORY_MIRROR_BRANCH_PREFIX", "gitfoam")

    import time as _t

    now = int(_t.time())
    fresh = now - 86400  # 1d old
    stale = now - (20 * 86400)  # 20d old

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[:2] == ["git", "fetch"]:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[:2] == ["git", "for-each-ref"]:
            return subprocess.CompletedProcess(
                cmd,
                0,
                f"origin/gitfoam/fresh-host/main {fresh}\n"
                f"origin/gitfoam/stale-merged/main {stale}\n"
                f"origin/gitfoam/stale-unmerged/main {stale}\n",
                "",
            )
        if cmd[:2] == ["git", "merge-base"]:
            # cmd is: ["git", "merge-base", "--is-ancestor", <branch>, <main-ref>]
            # Only stale-merged is ancestor of main. Others are not.
            branch_arg = cmd[3] if len(cmd) > 3 else ""
            if "stale-merged" in branch_arg:
                return subprocess.CompletedProcess(cmd, 0, "", "")
            return subprocess.CompletedProcess(cmd, 1, "", "")
        if cmd[:3] == ["git", "push", "origin"] and "--delete" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(mm, "_run", fake_run)
    deleted = mm.sweep_branches(idle_days=15)
    assert deleted == 1

    deletes = [c for c in calls if c[:3] == ["git", "push", "origin"] and "--delete" in c]
    assert len(deletes) == 1
    assert any("gitfoam/stale-merged/main" in c for c in deletes[0])


# ---------------------------------------------------------------------------
# Config back-compat: legacy MEMORY_MIRROR_ENABLED=true → mode=write.
# ---------------------------------------------------------------------------


def test_config_mode_derives_from_legacy_enabled_flag(monkeypatch):
    from hooks import config as cfg

    monkeypatch.setenv("MEMORY_MIRROR_ENABLED", "true")
    monkeypatch.delenv("MEMORY_MIRROR_MODE", raising=False)
    importlib.reload(cfg)
    assert cfg.MEMORY_MIRROR_MODE == "write"
    assert cfg.MEMORY_MIRROR_ENABLED is True


def test_config_mode_explicit_overrides_legacy(monkeypatch):
    from hooks import config as cfg

    monkeypatch.setenv("MEMORY_MIRROR_ENABLED", "true")
    monkeypatch.setenv("MEMORY_MIRROR_MODE", "write-local-only")
    importlib.reload(cfg)
    assert cfg.MEMORY_MIRROR_MODE == "write-local-only"


def test_config_mode_defaults_off(monkeypatch):
    from hooks import config as cfg

    # Strip both the .env-loaded value and the shell env so we see the true default.
    monkeypatch.delenv("MEMORY_MIRROR_MODE", raising=False)
    monkeypatch.delenv("MEMORY_MIRROR_ENABLED", raising=False)
    # Prevent the module-level .env loader from re-setting these from ~/.agentihooks/*.env
    # by pointing AGENTIHOOKS_HOME at an empty tmp dir.
    monkeypatch.setenv("AGENTIHOOKS_HOME", os.environ.get("AGENTIHOOKS_HOME_TEST", "/tmp/__agentihooks_empty__"))
    Path("/tmp/__agentihooks_empty__").mkdir(parents=True, exist_ok=True)
    importlib.reload(cfg)
    assert cfg.MEMORY_MIRROR_MODE == "off"
    assert cfg.MEMORY_MIRROR_ENABLED is False
