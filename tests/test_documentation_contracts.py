import json

import pytest

from hooks.context import documentation_evidence as evidence
from hooks.context import documentation_session as session
from tests.test_documentation_evidence import git, repository
from tests.test_documentation_session import answer
from tests.test_documentation_session import world as world


@pytest.mark.parametrize("context", [{"description": "WidgetPolicy"}, {"title": "WidgetPolicy"}, {}])
def test_source_requirements_retrieve_docs_without_a_code_diff(tmp_path, context):
    base = repository(tmp_path)
    result = evidence.collect(tmp_path, base, context)
    assert result["changes"] == ""
    assert result["source_task"] == context
    assert bool(result["docs"]) is bool(context)
    assert result["summary"] == "Baseline"


def test_evidence_uses_delivered_branch_and_ignores_dirty_checkout(tmp_path):
    base = repository(tmp_path)
    (tmp_path / "policy.py").write_text("removed_setting = False\n")
    git(tmp_path, "add", "policy.py")
    git(tmp_path, "commit", "-qm", "Delivered policy")
    git(tmp_path, "branch", "delivered")
    delivered = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "branch", "policy.py")
    (tmp_path / "unrelated.py").write_text("other = True\n")
    git(tmp_path, "add", "unrelated.py")
    (tmp_path / "policy.py").write_text("unrelated_setting = True\n")
    git(tmp_path, "add", "policy.py")
    git(tmp_path, "commit", "-qm", "Unrelated policy")
    (tmp_path / "policy.py").write_text("dirty_setting = True\n")
    result = evidence.collect(tmp_path, base, {"branch": "delivered"})
    assert result["head"] == delivered
    assert result["files"] == ["policy.py"]
    assert result["summary"] == "Delivered policy"
    assert "removed_setting" in result["changes"]
    assert "unrelated_setting" not in result["changes"]
    assert "dirty_setting" not in result["changes"]
    fallback = evidence.collect(tmp_path, "", {"branch": "absent"})
    assert fallback["head"] == git(tmp_path, "rev-parse", "HEAD")
    assert fallback["changes"] == ""


def test_documentation_retrieval_excludes_doctrine_and_oversized_sources(tmp_path):
    base = repository(tmp_path)
    for name in ("AGENTS.md", "CLAUDE.md"):
        (tmp_path / name).write_text("WidgetPolicy")
    (tmp_path / "boundary.md").write_text("WidgetPolicy" + " " * (131072 - len("WidgetPolicy")))
    (tmp_path / "oversized.md").write_text("WidgetPolicy" + " " * 131072)
    git(tmp_path, "add", "AGENTS.md", "CLAUDE.md", "boundary.md", "oversized.md")
    git(tmp_path, "commit", "-qm", "Document policy")
    result = evidence.collect(tmp_path, base)
    assert result["doc_sources"] == ["boundary.md", "handbook/policy.md"]


def test_regex_retrieval_bounds_terms_matches_and_excerpt_lengths(tmp_path):
    source = tmp_path / "guide.md"
    source.write_text("\n".join(f"word{index:04d}" for index in range(33)))
    query = " ".join(f"word{index:04d}" for index in range(33))
    assert len(evidence.matches(tmp_path, [source.name], query)) == 32
    source.write_text("\n".join("WIDGET " + "x" * 300 for _ in range(81)))
    matches = evidence.matches(tmp_path, [source.name], "widget")
    assert len(matches) == 80
    assert matches[-1]["line"] == 80
    assert all(len(match["text"]) == 256 for match in matches)
    assert evidence.matches(tmp_path, [source.name], "a 12") == []


def test_retrieval_limits_source_count_and_changed_file_diff(tmp_path):
    base = repository(tmp_path)
    for index in range(201):
        (tmp_path / f"guide{index:03d}.md").write_text("WidgetPolicy\n")
    for index in range(81):
        (tmp_path / f"code{index:03d}.py").write_text(f"setting{index:03d} = True\n")
    git(
        tmp_path,
        "add",
        "--",
        *[p.name for p in tmp_path.glob("guide*.md")],
        *[p.name for p in tmp_path.glob("code*.py")],
    )
    git(tmp_path, "commit", "-qm", "Add settings and guides")
    result = evidence.collect(tmp_path, base)
    assert len(result["doc_sources"]) == 200
    assert "code079.py" in result["changes"]
    assert "code080.py" not in result["changes"]
    assert len(result["files"]) == 282


def test_queue_request_preserves_requirements_and_deduplicates_across_sessions(world, monkeypatch):
    _, payload, ledger, jobs = world
    ledger.source.update(
        title="Retry control command", description="Explain retry limits", pr_url="https://example.test/pull/1"
    )
    monkeypatch.setattr(session.documentation.runner, "decide", answer())
    calls = []
    original_lookup = ledger.task

    def lookup(slug, task_id):
        assert slug == "fixture"
        assert task_id
        return original_lookup(slug, task_id)

    monkeypatch.setattr(ledger, "task", lookup)
    original = ledger.add_task

    def add(slug, fields, by):
        calls.append((slug, by))
        original(slug, fields, by)

    monkeypatch.setattr(ledger, "add_task", add)
    session.end(payload)
    fn, args = jobs.pop()
    fn(*args)
    row = json.loads(args[0].read_text())
    task = ledger.added[0]
    assert task["task"] == row["documentation_task"]
    assert task["task"].startswith("doc-") and len(task["task"]) == 20
    assert task["title"] == "Document Retry control command"
    assert "Document the delivered behavior of Retry control command.\n" in task["description"]
    assert 'Documentation sources: ["handbook/policy.md"]\n' in task["description"]
    assert task["follow_up"] is True
    assert task["not_duplicate"] == "Documents delivered behavior rather than implementing it"
    assert "Explain retry limits" in task["description"]
    assert "https://example.test/pull/1" in task["description"]
    assert "WidgetPolicy controls retries." in task["description"]
    assert calls == [("fixture", "swarm")]
    row.update(session="successor", status="pending")
    session._write(args[0], row)
    fn(*args)
    assert len(ledger.added) == 1


def test_session_snapshot_is_not_overwritten_and_missing_end_snapshot_is_created(world):
    root, payload, _, jobs = world
    path = session._path(payload["session_id"])
    original = json.loads(path.read_text())
    session.start(payload)
    assert json.loads(path.read_text()) == original
    assert original["root"] == str(root)
    assert original["status"] == "active"
    fresh = dict(payload, session_id="late-snapshot")
    session.end(fresh)
    assert json.loads(session._path(fresh["session_id"]).read_text())["status"] == "pending"
    assert len(jobs) == 1


def test_classifier_child_never_creates_a_session_snapshot(world, monkeypatch):
    _, payload, _, jobs = world
    monkeypatch.setenv("AGENTIHOOKS_CLASSIFIER_CHILD", "1")
    payload = dict(payload, session_id="classifier-child")
    session.start(payload)
    session.end(payload)
    assert not session._path(payload["session_id"]).exists()
    assert jobs == []


def test_failed_worker_keeps_error_and_reuses_a_completed_classification(world, monkeypatch):
    _, payload, ledger, jobs = world
    monkeypatch.setattr(session.documentation.runner, "decide", answer())
    original = ledger.add_task

    def fail(*args, **kwargs):
        raise ConnectionError("offline")

    monkeypatch.setattr(ledger, "add_task", fail)
    session.end(payload)
    fn, args = jobs.pop()
    fn(*args)
    row = json.loads(args[0].read_text())
    assert row["status"] == "pending"
    assert row["error"] == "ConnectionError"
    assert row["verdict"]["needs_documentation"] is True

    def no_reclassification(*args, **kwargs):
        raise AssertionError("reuse the completed classifier verdict")

    monkeypatch.setattr(session.documentation, "classify", no_reclassification)
    monkeypatch.setattr(ledger, "add_task", original)
    fn(*args)
    assert len(ledger.added) == 1
    assert json.loads(args[0].read_text())["status"] == "queued"


def test_removed_worktree_uses_repository_for_retained_request(world, monkeypatch):
    root, payload, _, jobs = world
    monkeypatch.setattr(session.documentation.runner, "decide", answer())
    path = session._path(payload["session_id"])
    row = json.loads(path.read_text())
    row["root"] = str(root / "gone")
    session._write(path, row)
    session.end(payload)
    fn, args = jobs.pop()
    fn(*args)
    assert json.loads(path.read_text())["status"] == "queued"


def test_missing_identity_and_non_repository_end_do_not_launch_workers(world, monkeypatch):
    root, _, _, jobs = world
    logged = []
    monkeypatch.setattr(session, "log", lambda *args: logged.append(args))
    session.start({"cwd": str(root)})
    session.end({"cwd": str(root)})
    session.end({"session_id": "missing-directory", "cwd": str(root / "missing")})
    assert jobs == []
    assert logged == [("documentation snapshot unavailable", {"error": "FileNotFoundError"})]


def test_source_and_commit_metadata_are_redacted_before_classification(tmp_path):
    base = repository(tmp_path)
    marker = "fixture" * 8
    subject = "API_TOKEN=" + marker
    (tmp_path / "policy.py").write_text("enabled = True\n")
    git(tmp_path, "add", "policy.py")
    git(tmp_path, "commit", "-qm", subject)
    result = evidence.collect(tmp_path, base, {"description": subject})
    assert marker not in result["summary"]
    assert marker not in json.dumps(result["source_task"])
    assert result["source_task"]["description"]


def test_credential_paths_with_documentation_extensions_are_never_sources(tmp_path):
    base = repository(tmp_path)
    (tmp_path / ".env.md").write_text("WidgetPolicy configuration\n")
    git(tmp_path, "add", ".env.md")
    git(tmp_path, "commit", "-qm", "Add private configuration")
    result = evidence.collect(tmp_path, base, {"description": "WidgetPolicy"})
    assert ".env.md" not in result["files"]
    assert ".env.md" not in result["doc_sources"]
    assert all(match["path"] != ".env.md" for match in result["docs"])


def test_removed_identifiers_remain_complete_retrieval_terms(tmp_path):
    repository(tmp_path)
    (tmp_path / "policy.py").write_text("WidgetPolicy\n")
    git(tmp_path, "add", "policy.py")
    git(tmp_path, "commit", "-qm", "Original policy")
    base = git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "policy.py").write_text("ReplacementPolicy\n")
    git(tmp_path, "add", "policy.py")
    git(tmp_path, "commit", "-qm", "Replace policy")
    result = evidence.collect(tmp_path, base)
    assert result["docs"] == [{"path": "handbook/policy.md", "line": 2, "text": "WidgetPolicy controls retries."}]


def test_short_assignments_are_redacted_from_diffs_and_documentation(tmp_path):
    base = repository(tmp_path)
    marker = "demo"
    assignment = "API_TOKEN=" + marker
    (tmp_path / "policy.py").write_text("WidgetPolicy\n" + assignment + "\n")
    (tmp_path / "handbook/policy.md").write_text("WidgetPolicy " + assignment + "\n")
    git(tmp_path, "add", "policy.py", "handbook/policy.md")
    git(tmp_path, "commit", "-qm", "Configure policy")
    result = evidence.collect(tmp_path, base)
    assert marker not in result["changes"]
    assert result["docs"]
    assert all(marker not in row["text"] for row in result["docs"])


def test_missing_requirements_do_not_search_placeholder_documentation(tmp_path):
    base = repository(tmp_path)
    (tmp_path / "handbook/policy.md").write_text("XXXX is a redacted placeholder.\n")
    git(tmp_path, "add", "handbook/policy.md")
    git(tmp_path, "commit", "-qm", "Clarify placeholders")
    head = git(tmp_path, "rev-parse", "HEAD")
    result = evidence.collect(tmp_path, head)
    assert result["docs"] == []
    assert base != head
