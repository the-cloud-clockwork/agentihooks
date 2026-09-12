"""Project bridge — repo discovery, the repo skills root, and the rules banner."""

import os
import subprocess
from pathlib import Path

import pytest

from hooks.context import project_bridge


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "proj"
    (root / ".claude" / "rules").mkdir(parents=True)
    (root / ".claude" / "skills" / "demo").mkdir(parents=True)
    (root / ".claude" / "rules" / "b-second.md").write_text("second body")
    (root / ".claude" / "rules" / "a-first.md").write_text("first body")
    _git("init", "-q", cwd=root)
    return root


class TestRepoRoot:
    def test_git_repo_with_claude_dir(self, repo):
        assert project_bridge._repo_root(str(repo)) == repo

    def test_subdirectory_resolves_to_root(self, repo):
        sub = repo / "src" / "deep"
        sub.mkdir(parents=True)
        assert project_bridge._repo_root(str(sub)) == repo

    def test_non_git_dir_walks_up_to_claude(self, tmp_path):
        root = tmp_path / "plain"
        (root / ".claude").mkdir(parents=True)
        sub = root / "a" / "b"
        sub.mkdir(parents=True)
        assert project_bridge._repo_root(str(sub)) == root

    def test_no_claude_anywhere(self, tmp_path):
        bare = tmp_path / "bare"
        bare.mkdir()
        assert project_bridge._repo_root(str(bare)) is None

    def test_missing_cwd(self):
        assert project_bridge._repo_root("/nonexistent/path/xyz") is None


class TestSkillsRoot:
    def test_creates_relative_symlink(self, repo):
        project_bridge.ensure_skills_root(repo)
        link = repo / ".agents" / "skills"
        assert link.is_symlink()
        assert os.readlink(link) == "../.claude/skills"
        assert (link / "demo").is_dir()

    def test_idempotent(self, repo):
        project_bridge.ensure_skills_root(repo)
        project_bridge.ensure_skills_root(repo)
        assert os.readlink(repo / ".agents" / "skills") == "../.claude/skills"

    def test_refuses_to_replace_a_real_directory(self, repo):
        real = repo / ".agents" / "skills"
        real.mkdir(parents=True)
        (real / "operator-skill").mkdir()
        project_bridge.ensure_skills_root(repo)
        assert not real.is_symlink()
        assert (real / "operator-skill").is_dir()

    def test_no_claude_skills_is_a_noop(self, tmp_path):
        root = tmp_path / "norules"
        (root / ".claude").mkdir(parents=True)
        project_bridge.ensure_skills_root(root)
        assert not (root / ".agents").exists()

    def test_excluded_locally_not_via_gitignore(self, repo):
        project_bridge.ensure_skills_root(repo)
        exclude = repo / ".git" / "info" / "exclude"
        assert "/.agents/" in exclude.read_text().split("\n")
        assert not (repo / ".gitignore").exists()

    def test_exclude_entry_written_once(self, repo):
        project_bridge.ensure_skills_root(repo)
        (repo / ".agents" / "skills").unlink()
        project_bridge.ensure_skills_root(repo)
        exclude = (repo / ".git" / "info" / "exclude").read_text()
        assert exclude.count("/.agents/") == 1

    def test_worktree_excludes_into_the_common_git_dir(self, repo, tmp_path):
        _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init", cwd=repo)
        wt = tmp_path / "wt"
        _git("worktree", "add", "-q", str(wt), "-b", "side", cwd=repo)
        (wt / ".claude" / "skills").mkdir(parents=True, exist_ok=True)
        project_bridge.ensure_skills_root(wt)
        assert "/.agents/" in (repo / ".git" / "info" / "exclude").read_text()


class TestBanner:
    def test_rules_in_sorted_order_under_the_hard_floor_header(self, repo, monkeypatch):
        monkeypatch.setattr(project_bridge, "_memory_file", lambda _root: None)
        banner = project_bridge.build_banner(repo)
        assert banner.startswith("RULES IMPORTANT!! (HARD FLOOR) !!")
        assert banner.index("a-first.md") < banner.index("b-second.md")
        assert "first body" in banner and "second body" in banner

    def test_project_memory_appended_last(self, repo, tmp_path, monkeypatch):
        mem = tmp_path / "MEMORY.md"
        mem.write_text("remembered fact")
        monkeypatch.setattr(project_bridge, "_memory_file", lambda _root: mem)
        banner = project_bridge.build_banner(repo)
        assert banner.index("--- project memory ---") > banner.index("b-second.md")
        assert "remembered fact" in banner

    def test_empty_repo_yields_no_banner(self, tmp_path, monkeypatch):
        monkeypatch.setattr(project_bridge, "_memory_file", lambda _root: None)
        root = tmp_path / "empty"
        (root / ".claude").mkdir(parents=True)
        assert project_bridge.build_banner(root) == ""

    def test_readme_is_not_a_rule(self, repo, monkeypatch):
        monkeypatch.setattr(project_bridge, "_memory_file", lambda _root: None)
        (repo / ".claude" / "rules" / "README.md").write_text("not a rule")
        assert "not a rule" not in project_bridge.build_banner(repo)

    def test_budget_truncates_with_a_trailer(self, repo, monkeypatch):
        monkeypatch.setattr(project_bridge, "_memory_file", lambda _root: None)
        full = project_bridge.build_banner(repo)
        budget = len(full.encode()) - 20
        banner = project_bridge.build_banner(repo, budget=budget)
        assert "second body" not in banner
        assert banner.startswith("RULES IMPORTANT!! (HARD FLOOR) !!")
        assert "TRUNCATED" in banner


class TestInjection:
    def test_injects_once_and_skips_compression(self, repo, monkeypatch):
        monkeypatch.setenv("AGENTIHOOKS_TARGET", "codex")
        monkeypatch.setattr(project_bridge, "_memory_file", lambda _root: None)
        calls = []
        monkeypatch.setattr(
            project_bridge,
            "inject_context",
            lambda content, also_log=True, skip_compression=False: calls.append((content, skip_compression)),
        )
        project_bridge.inject_project_context(str(repo))
        assert len(calls) == 1
        content, skip_compression = calls[0]
        assert skip_compression is True
        assert "first body" in content

    def test_silent_outside_a_claude_repo(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGENTIHOOKS_TARGET", "codex")
        calls = []
        monkeypatch.setattr(
            project_bridge,
            "inject_context",
            lambda *a, **k: calls.append(a),
        )
        bare = tmp_path / "bare"
        bare.mkdir()
        project_bridge.inject_project_context(str(bare))
        assert calls == []


class TestTargetGates:
    def test_claude_is_a_noop_it_loads_the_tree_itself(self, repo, monkeypatch):
        monkeypatch.setenv("AGENTIHOOKS_TARGET", "claude")
        calls = []
        monkeypatch.setattr(project_bridge, "inject_context", lambda *a, **k: calls.append(a))
        project_bridge.inject_project_context(str(repo))
        assert calls == []
        assert not (repo / ".agents").exists()

    def test_copilot_injects_without_the_skills_link(self, repo, monkeypatch):
        monkeypatch.setenv("AGENTIHOOKS_TARGET", "copilot")
        monkeypatch.setattr(project_bridge, "_memory_file", lambda _root: None)
        calls = []
        monkeypatch.setattr(
            project_bridge,
            "inject_context",
            lambda content, also_log=True, skip_compression=False: calls.append(content),
        )
        project_bridge.inject_project_context(str(repo))
        assert len(calls) == 1 and "first body" in calls[0]
        # copilot scans .claude/skills itself — a second root would be noise.
        assert not (repo / ".agents").exists()

    def test_codex_injects_and_links(self, repo, monkeypatch):
        monkeypatch.setenv("AGENTIHOOKS_TARGET", "codex")
        monkeypatch.setattr(project_bridge, "_memory_file", lambda _root: None)
        monkeypatch.setattr(project_bridge, "inject_context", lambda *a, **k: None)
        project_bridge.inject_project_context(str(repo))
        assert (repo / ".agents" / "skills").is_symlink()


class TestMemoryResolution:
    def test_memory_path_follows_the_claude_project_encoding(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
        from hooks.context.broadcast import encode_cwd

        root = tmp_path / "dev" / "thing"
        root.mkdir(parents=True)
        mem = tmp_path / ".claude" / "projects" / encode_cwd(str(root)) / "memory"
        mem.mkdir(parents=True)
        (mem / "MEMORY.md").write_text("x")
        assert project_bridge._memory_file(root) == mem / "MEMORY.md"
