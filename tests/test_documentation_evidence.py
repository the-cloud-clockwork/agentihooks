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


def pull(root, name, text):
    (root / name).write_text(text)
    git(root, "add", name)
    tree = git(root, "write-tree")
    commit = git(root, "commit-tree", tree, "-p", "HEAD", "-m", f"Add {name}")
    git(root, "update-ref", "refs/remotes/origin/dev", commit)
    git(root, "merge", "-q", "--ff-only", "origin/dev")


def test_commits_pulled_from_dev_are_excluded_from_the_delivered_changes(tmp_path):
    base = repository(tmp_path)
    pull(tmp_path, "pulled.py", "class PulledWidget:\n    size = 1\n")
    assert documentation_evidence.collect(tmp_path, base)["files"] == []
    (tmp_path / "policy.py").write_text("class WidgetPolicy:\n    retries = 3\n")
    git(tmp_path, "add", "policy.py")
    git(tmp_path, "commit", "-qm", "Add retry policy")
    result = documentation_evidence.collect(tmp_path, base)
    assert result["files"] == ["policy.py"]
    assert "PulledWidget" not in result["changes"]


def test_commits_the_session_authored_and_pushed_to_dev_stay_delivered(tmp_path):
    base = repository(tmp_path)
    (tmp_path / "policy.py").write_text("class WidgetPolicy:\n    retries = 3\n")
    git(tmp_path, "add", "policy.py")
    git(tmp_path, "commit", "-qm", "Add retry policy")
    git(tmp_path, "update-ref", "refs/remotes/origin/dev", "HEAD")
    assert documentation_evidence.collect(tmp_path, base)["files"] == ["policy.py"]


def test_a_pull_that_merges_dev_onto_local_work_excludes_the_pulled_changes(tmp_path):
    baseline = repository(tmp_path)
    git(tmp_path, "update-ref", "refs/remotes/origin/dev", baseline)
    (tmp_path / "local.py").write_text("class LocalWidget:\n    size = 1\n")
    git(tmp_path, "add", "local.py")
    git(tmp_path, "commit", "-qm", "Add local widget")
    base = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "checkout", "-q", "-b", "upstream", baseline)
    (tmp_path / "pulled.py").write_text("class PulledWidget:\n    size = 1\n")
    git(tmp_path, "add", "pulled.py")
    git(tmp_path, "commit", "-qm", "Add pulled widget")
    git(tmp_path, "update-ref", "refs/remotes/origin/dev", "HEAD")
    git(tmp_path, "checkout", "-q", "-")
    git(tmp_path, "merge", "-q", "--no-edit", "origin/dev")
    (tmp_path / "policy.py").write_text("class WidgetPolicy:\n    retries = 3\n")
    git(tmp_path, "add", "policy.py")
    git(tmp_path, "commit", "-qm", "Add retry policy")
    result = documentation_evidence.collect(tmp_path, base)
    assert "pulled.py" not in result["files"]
    assert "policy.py" in result["files"]
    assert "PulledWidget" not in result["changes"]


def test_a_session_base_ahead_of_dev_keeps_its_session_base(tmp_path):
    git(tmp_path, "update-ref", "refs/remotes/origin/dev", repository(tmp_path))
    (tmp_path / "local.py").write_text("class LocalWidget:\n    size = 1\n")
    git(tmp_path, "add", "local.py")
    git(tmp_path, "commit", "-qm", "Add local widget")
    base = git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "policy.py").write_text("class WidgetPolicy:\n    retries = 3\n")
    git(tmp_path, "add", "policy.py")
    git(tmp_path, "commit", "-qm", "Add retry policy")
    assert documentation_evidence.collect(tmp_path, base)["files"] == ["policy.py"]


def test_an_unknown_revision_is_not_an_ancestor_and_prints_nothing(tmp_path, capfd):
    repository(tmp_path)
    assert documentation_evidence._is_ancestor(tmp_path, "0" * 40, "HEAD") is False
    assert capfd.readouterr().err == ""


PULL_REQUEST = "https://example.test/pull/7"
PULL_REQUEST_DIFF = (
    "diff --git a/policy.py b/policy.py\n--- a/policy.py\n+++ b/policy.py\n@@ -1 +1,2 @@\n"
    "-enabled = False\n+class WidgetPolicy:\n+    retries = 3\n"
    "diff --git a/.env b/.env\n--- a/.env\n+++ b/.env\n@@ -0,0 +1 @@\n+CREDENTIAL_SENTINEL\n"
)


def unrelated(root):
    (root / "login.py").write_text("class LoginRecovery:\n    attempts = 2\n")
    git(root, "add", "login.py")
    git(root, "commit", "-qm", "Recover the master login")


def test_a_source_task_takes_its_changes_from_its_own_pull_request(tmp_path, monkeypatch):
    base = repository(tmp_path)
    unrelated(tmp_path)
    asked = []
    monkeypatch.setattr(documentation_evidence, "pull_request_diff", lambda url: asked.append(url) or PULL_REQUEST_DIFF)
    result = documentation_evidence.collect(tmp_path, base, {"id": "t1", "pr_url": PULL_REQUEST})
    assert asked == [PULL_REQUEST]
    assert result["files"] == ["policy.py"]
    assert "+class WidgetPolicy:" in result["changes"]
    assert "LoginRecovery" not in result["changes"]
    assert "CREDENTIAL_SENTINEL" not in result["changes"]
    assert result["docs"] == [{"path": "handbook/policy.md", "line": 2, "text": "WidgetPolicy controls retries."}]


def test_a_source_task_without_a_pull_request_gets_no_diff(tmp_path, monkeypatch):
    base = repository(tmp_path)
    unrelated(tmp_path)

    def forbidden(url):
        raise AssertionError("no pull request to read")

    monkeypatch.setattr(documentation_evidence, "pull_request_diff", forbidden)
    result = documentation_evidence.collect(tmp_path, base, {"id": "t1", "pr_url": ""})
    assert (result["files"], result["changes"]) == ([], "")


def test_the_pull_request_diff_is_read_from_the_named_pull_request(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "check_output", lambda args, **kwargs: calls.append(args) or "")
    assert documentation_evidence.pull_request_diff(PULL_REQUEST) == ""
    assert calls == [["gh", "pr", "diff", PULL_REQUEST, "--color", "never"]]
