"""The sources manifest a profile render writes: one trace row per rendered rule and doctrine file."""

import json
from pathlib import Path

import pytest

from scripts.profiles import sources
from tests.test_profile_render import _commit, _git, _write, world  # noqa: F401


@pytest.fixture
def bundle(world, monkeypatch):  # noqa: F811
    for name in ("CI_MANIFESTO_PATH", "MANIFESTOS_DIR", "CI_MANIFESTO_SKIP"):
        monkeypatch.delenv(name, raising=False)
    _write(world["bundle"] / "manifestos" / "CI.md", "\n# CI Manifesto\nbody\n")
    _commit(world["bundle"], "manifesto")
    return world["bundle"]


def _blob(repo, rel):
    return _git(repo, "hash-object", rel).strip()


def _by_source(rows):
    return {row["source"]: row for row in rows}


def test_claude_render_writes_a_row_per_rule_and_doctrine_file(bundle):
    from scripts.profiles import render

    render.render_claude("rb-role")
    rows = json.loads(sources.path("rb-role", "claude", render.rendered_root()).read_text())
    found = _by_source(row for row in rows if row["locator"]["repo"] == str(bundle))
    packaged = [row for row in rows if row["locator"]["repo"] != str(bundle)]
    assert [(row["layer"], Path(row["locator"]["path"]).name) for row in packaged] == [
        ("rule", "agentihooks-toolbelt.md")
    ]
    assert {source: row["layer"] for source, row in found.items()} == {
        "bundle/.claude/CLAUDE.md": "doctrine",
        "bundle/profiles/rb-base/CLAUDE.md": "doctrine",
        "bundle/profiles/rb-role/CLAUDE.md": "doctrine",
        "bundle/manifestos/CI.md": "doctrine",
        "bundle/.claude/rules/bundle-rule.md": "rule",
        "bundle/profiles/rb-kit/.claude/rules/role-rule.md": "rule",
    }
    rule = found["bundle/profiles/rb-kit/.claude/rules/role-rule.md"]
    assert rule["locator"] == {
        "repo": str(bundle),
        "path": "profiles/rb-kit/.claude/rules/role-rule.md",
        "blob": _blob(bundle, "profiles/rb-kit/.claude/rules/role-rule.md"),
    }
    assert rule["text"] == "ROLE RULE MARKER"
    assert found["bundle/manifestos/CI.md"]["text"] == "# CI Manifesto"


def test_codex_render_writes_the_same_rows(bundle):
    from scripts.profiles import render

    render.render_claude("rb-role")
    render.render_codex("rb-role")
    root = render.rendered_root()
    claude = json.loads(sources.path("rb-role", "claude", root).read_text())
    codex = json.loads(sources.path("rb-role", "codex", root).read_text())
    assert codex == claude and len(codex) == 7


@pytest.mark.parametrize("target", ["claude", "codex"])
def test_a_render_without_its_manifest_renders_again(bundle, target):
    from scripts.profiles import render

    assert render.render(target, "rb-role") is not None
    assert render.render(target, "rb-role") is None
    sources.path("rb-role", target, render.rendered_root()).unlink()
    assert render.render(target, "rb-role") is not None
    assert sources.path("rb-role", target, render.rendered_root()).is_file()


def test_a_file_outside_any_repo_is_located_by_its_absolute_path(tmp_path):
    loose = _write(tmp_path / "loose" / "rule.md", "\n\n  first words  \nmore\n")
    row = sources.row("rule", loose)
    assert row["source"] == str(loose)
    assert row["locator"] == {"repo": "", "path": str(loose), "blob": sources.blob(loose.read_bytes())}
    assert row["text"] == "first words"


def test_blob_is_the_git_blob_id(tmp_path):
    _write(tmp_path / "r" / "a.md", "same bytes git hashes\n")
    _git(tmp_path / "r", "init", "-q")
    assert sources.blob((tmp_path / "r" / "a.md").read_bytes()) == _blob(tmp_path / "r", "a.md")


def test_a_worktree_file_names_the_worktree_as_its_repo(tmp_path):
    repo = tmp_path / "wt"
    _write(repo / ".git", "gitdir: /elsewhere\n")
    rule = _write(repo / "rules" / "x.md", "X\n")
    row = sources.row("rule", rule)
    assert row["source"] == "wt/rules/x.md"
    assert row["locator"]["repo"] == str(repo) and row["locator"]["path"] == "rules/x.md"


def test_empty_doctrine_files_are_left_out(bundle):
    from scripts.profiles import render

    _write(bundle / "profiles" / "rb-base" / "CLAUDE.md", "  \n")
    render.render_claude("rb-role", force=True)
    rows = json.loads(sources.path("rb-role", "claude", render.rendered_root()).read_text())
    assert "bundle/profiles/rb-base/CLAUDE.md" not in _by_source(rows)


def test_an_empty_rule_file_has_empty_text_and_bad_bytes_are_replaced(tmp_path):
    empty = _write(tmp_path / "empty.md", "\n  \n")
    assert sources.row("rule", empty)["text"] == ""
    broken = tmp_path / "broken.md"
    broken.write_bytes(b"\n# Rule \xff marker\n")
    assert sources.row("rule", broken)["text"] == "# Rule � marker"


def test_a_bundle_without_a_shared_claude_dir_reads_its_root_claude_md(tmp_path):
    bundle = tmp_path / "b"
    _write(bundle / "CLAUDE.md", "ROOT DIRECTIVE\n")
    _write(bundle / ".claude" / "claude.md", "WRONG CASE\n")
    assert sources.doctrine_files(bundle, []) == [bundle / "CLAUDE.md"]


def _correct_passage(bundle, rel, quote):
    from hooks.context import injection_trace

    source = f"bundle/{rel}"
    injection_trace.record("sess-render", "rule", source, quote, {"repo": str(bundle), "path": rel, "blob": ""})
    injection_trace.correct("sess-render", source, "/repos/qitp", "belongs to another repo", quote)


def test_a_confirmed_correction_renders_its_notice_and_rerenders(bundle, monkeypatch):
    import tomllib

    from scripts.profiles import render
    from scripts.targets.codex_target import codex_home

    monkeypatch.delenv("AGENTIHOOKS_GATE_QUARANTINE", raising=False)
    out = render.render_claude("rb-role")
    assert render.render_claude("rb-role") is None
    _correct_passage(bundle, "profiles/rb-kit/.claude/rules/role-rule.md", "ROLE RULE MARKER")
    _correct_passage(bundle, "profiles/rb-role/CLAUDE.md", "ROLE PERSONA MARKER")

    assert render.render_claude("rb-role") == out

    notice = "> CORRECTION: the passage above is marked wrong for the qitp repo: belongs to another repo."
    persona = (out / "CLAUDE.md").read_text()
    assert f"ROLE RULE MARKER\n\n{notice}" in persona
    assert f"ROLE PERSONA MARKER\n\n{notice}" in persona
    assert f"BUNDLE RULE MARKER\n\n{notice}" not in persona
    render.render_codex("rb-role")
    codex = tomllib.loads((codex_home() / "rb-role.config.toml").read_text())["developer_instructions"]
    assert f"ROLE RULE MARKER\n\n{notice}" in codex and f"ROLE PERSONA MARKER\n\n{notice}" in codex


def test_a_rule_corrected_twice_renders_as_the_held_notice_only(bundle, monkeypatch):
    import tomllib

    from scripts.profiles import render
    from scripts.targets.codex_target import codex_home

    monkeypatch.delenv("AGENTIHOOKS_GATE_QUARANTINE", raising=False)
    rel = "profiles/rb-kit/.claude/rules/role-rule.md"
    _correct_passage(bundle, rel, "ROLE RULE MARKER")
    _correct_passage(bundle, rel, "ROLE RULE MARKER")

    out = render.render_claude("rb-role")
    render.render_codex("rb-role")

    held = (
        f"> CORRECTION: bundle/{rel} was marked wrong more than once and is held whole until the operator releases it."
    )
    persona = (out / "CLAUDE.md").read_text()
    assert f"<!-- rule: role-rule.md (rule) -->\n{held}\n" in persona
    assert "<!-- rule: bundle-rule.md (rule) -->\nBUNDLE RULE MARKER\n" in persona
    assert "ROLE RULE MARKER" not in persona
    codex = tomllib.loads((codex_home() / "rb-role.config.toml").read_text())["developer_instructions"]
    assert f"<!-- rule: role-rule.md (rule) -->\n{held}" in codex
    assert "ROLE RULE MARKER" not in codex
