import subprocess

from hooks.context import documentation_evidence


def git(root, *args):
    return subprocess.check_output(
        ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", *args], cwd=root, text=True
    ).strip()


def repository(tmp_path):
    git(tmp_path, "init", "-q")
    (tmp_path / "policy.py").write_text("enabled = False\n")
    docs = tmp_path / "handbook"
    docs.mkdir()
    (docs / "policy.md").write_text("# Policy\nWidgetPolicy controls retries.\n")
    git(tmp_path, "add", "policy.py", "handbook/policy.md")
    git(tmp_path, "commit", "-qm", "Baseline")
    return git(tmp_path, "rev-parse", "HEAD")


def test_changed_code_retrieves_regex_matches_from_a_custom_documentation_source(tmp_path):
    base = repository(tmp_path)
    (tmp_path / "policy.py").write_text("class WidgetPolicy:\n    retries = 3\n")
    git(tmp_path, "add", "policy.py")
    git(tmp_path, "commit", "-qm", "Add retry policy")
    result = documentation_evidence.collect(tmp_path, base)
    assert result["files"] == ["policy.py"]
    assert "+class WidgetPolicy:" in result["changes"]
    assert result["docs"] == [{"path": "handbook/policy.md", "line": 2, "text": "WidgetPolicy controls retries."}]
    assert result["doc_sources"] == ["handbook/policy.md"]
    assert result["head"] == git(tmp_path, "rev-parse", "HEAD")


def test_credential_files_and_links_are_excluded_from_documentation_evidence(tmp_path):
    base = repository(tmp_path)
    private = tmp_path / ".env"
    private.write_text("CREDENTIAL_SENTINEL\n")
    (tmp_path / "handbook/linked.md").symlink_to(private)
    git(tmp_path, "add", "-f", ".env", "handbook/linked.md")
    git(tmp_path, "commit", "-qm", "Add unrelated fixture")
    result = documentation_evidence.collect(tmp_path, base)
    assert "CREDENTIAL_SENTINEL" not in str(result)
    assert "handbook/linked.md" not in result["doc_sources"]


def test_documentation_lines_are_redacted_before_the_evidence_is_truncated(tmp_path):
    base = repository(tmp_path)
    (tmp_path / "policy.py").write_text("class WidgetPolicy:\n    retries = 3\n")
    line = "WidgetPolicy " + "x" * 229 + " " + "ghp_" + "z" * 36
    (tmp_path / "handbook/policy.md").write_text(line + "\n")
    git(tmp_path, "add", "policy.py", "handbook/policy.md")
    git(tmp_path, "commit", "-qm", "Add documented retry policy")
    result = documentation_evidence.collect(tmp_path, base)
    excerpt = result["docs"][0]["text"]
    assert "ghp_" not in excerpt
    assert "REDACTED" in excerpt
    assert len(excerpt) <= 256
